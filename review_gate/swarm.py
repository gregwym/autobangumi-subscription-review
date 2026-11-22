"""Compare public swarm observations by exact infohash."""

import asyncio
import base64
import re
import time
from collections import defaultdict
from urllib.parse import parse_qs, urlsplit
from urllib.request import getproxies_environment

import httpx2
from bs4 import BeautifulSoup

from .batch import parse_review_release
from .mikan_series import TORRENT, episode_hash
from .opentrackr import BATCH, scrape, scrape_http
from .versions import version_traits

INDEX = "https://share.dmhy.org/topics/list"
MAX_BODY = 2 * 1024 * 1024


def magnet_hash(url):
    if not url.lower().startswith("magnet:?"):
        return None
    hashes = set()
    for xt in parse_qs(urlsplit(url).query).get("xt", ()):
        if not xt.lower().startswith("urn:btih:"):
            continue
        value = xt[9:].lower()
        if re.fullmatch("[a-f0-9]{40}", value):
            hashes.add(value)
        elif re.fullmatch("[a-z2-7]{32}", value):
            hashes.add(base64.b32decode(value.upper()).hex())
        else:
            return None
    return hashes.pop() if len(hashes) == 1 else None


def public_hash(candidate):
    url = candidate["url"]
    if m := TORRENT.fullmatch(url):
        return m[1].lower()
    digest = magnet_hash(url)
    if not digest:
        return None
    page = candidate.get("homepage") or ""
    if episode_hash(page) == digest or re.fullmatch(
        r"https?://(?:share\.dmhy\.org/topics/view/[^/?#]+|nyaa\.si/view/[0-9]+)", page
    ):
        return digest
    return None


def number(cell):
    value = cell.get_text(strip=True)
    if re.fullmatch(r"[0-9]{1,9}|[0-9]{1,3}(?:,[0-9]{3}){1,2}", value):
        return int(value.replace(",", ""))
    return None


def index_page(body, page):
    soup = BeautifulSoup(body, "html.parser")
    table = soup.select_one("table#topic_list")
    if not table:
        return {}, False
    headers = [t.get_text(strip=True) for t in table.select("thead th")]
    if "種子" not in headers or "下載" not in headers:
        return {}, False
    seed_col, peer_col = headers.index("種子"), headers.index("下載")
    rows = {}
    for row in table.select("tbody tr"):
        link = row.select_one('a[href^="magnet:?"]')
        cells = row.find_all("td", recursive=False)
        if not link or len(cells) <= max(seed_col, peer_col):
            continue
        key = magnet_hash(link["href"])
        seeds = number(cells[seed_col])
        if key and seeds is not None:
            rows[key] = {"seeds": seeds, "leechers": number(cells[peer_col])}
    next_path = f"/topics/list/page/{page + 1}"
    more = any(a["href"].partition("?")[0] == next_path for a in soup.select(".nav_title a[href]"))
    return rows, more


def search_term(candidate, rule):
    name = candidate["name"].casefold()
    titles = [rule.get("title_raw"), rule.get("official_title")]
    title = next((t for t in titles if t and t.casefold() in name), None)
    if not title:
        release = parse_review_release(candidate["name"])[1]
        if not release:
            return ""
        title = release.title_en or release.title_zh or release.title_jp or ""
    words = re.findall(r"[^\W_]+", title)[:5]
    group = re.split(r"[&＆]", candidate.get("group") or "")[-1].strip()
    if words and group and group.casefold() in name:
        words.extend(re.findall(r"[^\W_]+", group)[:2])
    return " ".join(words)[:120]


async def fetch_index(term, page):
    from module.network.request_url import get_shared_client

    client = await get_shared_client()
    url = INDEX if page == 1 else f"{INDEX}/page/{page}"
    params = {"keyword": term}
    async with (
        asyncio.timeout(6),
        client.stream("GET", url, params=params, follow_redirects=False) as resp,
    ):
        resp.raise_for_status()
        body = bytearray()
        async for chunk in resp.aiter_bytes(chunk_size=32768):
            if len(body) + len(chunk) > MAX_BODY:
                raise ValueError("DMHY index too large")
            body.extend(chunk)
    return await asyncio.to_thread(index_page, bytes(body), page)


def summarize(releases, *, single):
    # A version can contain several revisions of an episode. Its best observed
    # seed count represents that episode; an unobserved episode stays unknown.
    units = defaultdict(list)
    for i, release in enumerate(releases):
        counts = units[release["episode"] if single else i]
        counts.extend(report["seeds"] for report in release["reports"])
    observed = [max(counts) for counts in units.values() if counts]
    return {
        "total": len(units),
        "unknown": len(units) - len(observed),
        "zero": observed.count(0),
        "min": min(observed, default=None),
        "max": max(observed, default=None),
    }


class SwarmHealth:
    def __init__(self):
        self.pages = {}
        self.lock = asyncio.Lock()
        self.retry = 0
        self.tracker_cache = {}
        self.tracker_lock = asyncio.Lock()
        self.tracker_retry = 0

    async def page(self, term, page):
        key = (term, page)
        async with self.lock:
            now = time.time()
            if key in self.pages:
                checked, rows, more = self.pages[key]
                if now - checked < 900:
                    return checked, rows, more
            if now < self.retry:
                return now, {}, False
            try:
                rows, more = await fetch_index(term, page)
            except httpx2.HTTPError, TimeoutError, ValueError:
                self.retry = now + 60
                return now, {}, False
            if len(self.pages) >= 128:
                self.pages.pop(next(iter(self.pages)))
            result = time.time(), rows, more
            self.pages[key] = result
            return result

    async def tracker_stats(self, hashes):
        from module.conf import settings

        use_proxy = settings.proxy.enable or bool(
            {"all", "http", "https"} & getproxies_environment().keys()
        )
        async with self.tracker_lock:
            now = time.time()
            cached = {
                h: self.tracker_cache[h]
                for h in hashes
                if h in self.tracker_cache and now - self.tracker_cache[h]["checked"] < 900
            }
            missing = sorted(hashes - cached.keys())
            if now >= self.tracker_retry:
                try:
                    async with asyncio.timeout(12):
                        for offset in range(0, len(missing), BATCH):
                            batch = missing[offset : offset + BATCH]
                            try:
                                results = await scrape_http(batch)
                            except httpx2.HTTPError, OSError, TimeoutError, ValueError:
                                if use_proxy:
                                    raise
                                results = await scrape(batch)
                            checked = int(time.time())
                            for h, row in results.items():
                                cached[h] = {**row, "source": "OpenTrackr", "checked": checked}
                                self.tracker_cache[h] = cached[h]
                except httpx2.HTTPError, OSError, TimeoutError, ValueError:
                    self.tracker_retry = time.time() + 60
            while len(self.tracker_cache) > 4096:
                self.tracker_cache.pop(next(iter(self.tracker_cache)))
            return cached

    async def index_stats(self, searches):
        stats = {}
        # The choices render independently. A slow index may return partial observations.
        try:
            async with asyncio.timeout(15):
                for term, wanted in searches.items():
                    for page in range(1, 4):
                        if wanted <= stats.keys():
                            break
                        checked, rows, more = await self.page(term, page)
                        stats.update(
                            {
                                h: {**r, "source": "动漫花园", "checked": int(checked)}
                                for h, r in rows.items()
                                if h in wanted
                            }
                        )
                        if not more:
                            break
        except TimeoutError:
            pass
        return stats

    async def for_rule(self, rule, candidates):
        versions = defaultdict(dict)
        searches = defaultdict(set)
        hashes = set()
        for c in candidates:
            if not c.get("episode_key"):
                continue
            version = version_traits(c)[0]
            digest = public_hash(c)
            key = digest or c["id"]
            versions[version][key] = {
                "name": c["name"],
                "episode": c["episode"],
                "end": c.get("episode_end"),
                "revision": c.get("revision", 1),
            }
            if digest:
                hashes.add(digest)
                if term := search_term(c, rule):
                    searches[term].add(digest)
        index, tracker = await asyncio.gather(
            self.index_stats(searches), self.tracker_stats(hashes)
        )
        result = {}
        for version, candidates_by_hash in versions.items():
            releases = [
                row | {"reports": [source[h] for source in (index, tracker) if h in source]}
                for h, row in candidates_by_hash.items()
            ]
            single = all(r["episode"] is not None and r["end"] is None for r in releases)
            result[version] = {"releases": releases, "summary": summarize(releases, single=single)}
        return result
