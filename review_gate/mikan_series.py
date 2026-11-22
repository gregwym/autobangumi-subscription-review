"""Group reviewed releases by the series link on Mikan's public episode page."""

import asyncio
import re
import time
from collections import Counter, defaultdict
from urllib.parse import parse_qsl, urlsplit

import httpx2
from bs4 import BeautifulSoup

from .batch import bounded_batch, parse_review_release, project_batch
from .policy import fingerprint

EPISODE = re.compile(r"https://(?:mikanani\.me|mikanime\.tv)/Home/Episode/([a-fA-F0-9]{40})")
SERIES_PATH = re.compile(r"/Home/Bangumi/([1-9]\d{0,9})/?(?:#\d+)?")
TORRENT = re.compile(
    r"https://(?:mikanani\.me|mikanime\.tv)/Download/\d{8}/([a-fA-F0-9]{40})\.torrent"
)
PAGE_LIMIT = 1024 * 1024


def episode_hash(homepage):
    match = EPISODE.fullmatch(homepage or "")
    return match[1].lower() if match else None


def release_page(url, homepage):
    match = TORRENT.fullmatch(url or "")
    if match and match[1].lower() == episode_hash(homepage):
        return homepage
    return None


def feed_series(url):
    """Series identity of a dedicated Mikan feed, including whole-show feeds."""
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.netloc not in {"mikanani.me", "mikanime.tv"}
            or parts.path.lower() != "/rss/bangumi"
            or parts.fragment
        ):
            return None
        pairs = [(k.lower(), v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
        params = dict(pairs)
        if (
            len(params) != len(pairs)
            or set(params) not in ({"bangumiid"}, {"bangumiid", "subgroupid"})
            or not all(re.fullmatch(r"[1-9]\d{0,9}", v) for v in params.values())
        ):
            return None
        return int(params["bangumiid"])
    except ValueError:
        return None


def page_series(data, expected_hash=None):
    soup = BeautifulSoup(data, "html.parser")
    links = soup.select("p.bangumi-title a[href]")
    if links:
        title_ids, feed_ids = set(), set()
        for link in links:
            href = link["href"]
            if match := SERIES_PATH.fullmatch(href):
                title_ids.add(int(match[1]))
            elif href.startswith("/RSS/") and (sid := feed_series("https://mikanani.me" + href)):
                feed_ids.add(sid)
            else:
                return None
        return next(iter(title_ids)) if len(title_ids) == 1 and feed_ids <= title_ids else None
    # Only an authenticated release identity (matching torrent hash in the
    # public page) can distinguish an unclassified post from a broken page.
    title = soup.select_one("#sk-container .episode-header .episode-title")
    if (
        not expected_hash
        or not title
        or not title.get_text(strip=True)
        or not soup.select_one("#sk-container .bangumi-info")
        or soup.select_one('p.bangumi-title, #sk-container a[href*="/Home/Bangumi/"]')
    ):
        return None
    paths = [
        a["href"]
        for a in soup.select('#sk-container .leftbar-nav a.episode-btn[href^="/Download/"]')
    ]
    hashes = {
        m[1].lower() if (m := TORRENT.fullmatch("https://mikanani.me" + path)) else None
        for path in paths
    }
    return 0 if hashes == {expected_hash} else None


async def fetch_series(homepage):
    from module.network.request_url import get_shared_client

    client = await get_shared_client()
    body = bytearray()
    async with asyncio.timeout(8), client.stream("GET", homepage, follow_redirects=False) as resp:
        resp.raise_for_status()
        async for chunk in resp.aiter_bytes(chunk_size=32768):
            if len(body) + len(chunk) > PAGE_LIMIT:
                return None
            body.extend(chunk)
    return await asyncio.to_thread(page_series, bytes(body), episode_hash(homepage))


class MikanSeries:
    def __init__(self, store, rule_data):
        self.store, self.rule_data = store, rule_data
        self.pages = {}
        self.rules = {}
        self.owners = {}
        self.retry_at = {}
        self.unlisted_until = {}
        self.catalog = {}
        self.anchors = {}
        self.pending_pages = set()
        self.discovery_lock = asyncio.Lock()

    async def load(self):
        def read(db):
            pages = dict(db.execute("SELECT episode,series FROM mikan_episodes"))
            bindings = {
                rid: (fp, series) for rid, fp, series in db.execute("SELECT * FROM mikan_bindings")
            }
            return pages, bindings

        self.pages, self.rules = await self.store.call(read)

    async def resolve(self, homepage):
        key = episode_hash(homepage)
        if not key or key in self.pages or self.retry_at.get(key, 0) > time.monotonic():
            return self.pages.get(key)
        if self.unlisted_until.get(key, 0) > time.monotonic():
            return 0
        try:
            series = await fetch_series(homepage)
        except httpx2.HTTPError, TimeoutError, ValueError:
            series = None
        if series is None:
            self.retry_at[key] = time.monotonic() + 600
            return None
        if series == 0:
            self.unlisted_until[key] = time.monotonic() + 6 * 3600
            return 0
        self.unlisted_until.pop(key, None)
        await self.store.call(
            lambda db: db.execute(
                "INSERT OR REPLACE INTO mikan_episodes VALUES(?,?)", (key, series)
            ).close()
        )
        self.retry_at.pop(key, None)
        self.pages[key] = series
        return series

    def identity(self, rule):
        item = self.rules.get(rule.id)
        if item and item[0] == fingerprint(self.rule_data(rule)):
            return item[1]
        return None

    async def bind(self, rule, series):
        value = fingerprint(self.rule_data(rule)), series
        if self.rules.get(rule.id) != value:
            await self.store.call(
                lambda db: db.execute(
                    "INSERT OR REPLACE INTO mikan_bindings VALUES(?,?,?)", (rule.id, *value)
                ).close()
            )
            self.rules[rule.id] = value

    async def observe(self, rules, history, network, retained=()):
        rows = defaultdict(list)
        owners = defaultdict(set)
        for t in history:
            if t.bangumi_id in rules or t.bangumi_id is None:
                owners[t.url].add(t.bangumi_id)
                rows[t.bangumi_id].append(release_page(t.url, t.homepage))
        self.owners = {url: next(iter(ids)) for url, ids in owners.items() if len(ids) == 1}
        # A merge checks all releases, including review-only candidates. Their
        # URLs contribute evidence but cannot replace the host's canonical owner.
        for candidate in retained:
            if candidate["rule_id"] in rules:
                rows[candidate["rule_id"]].append(
                    release_page(candidate["url"], candidate.get("homepage"))
                )
        rows[None].extend(self.pending_pages)
        card_counts = Counter(
            (r.official_title, r.season) for r in rules.values() if not r.archived
        )
        for rid in rows:
            rows[rid] = list(dict.fromkeys(rows[rid]))

        def priority(rid):
            r = rules.get(rid)
            waiting = r and not r.archived and card_counts[r.official_title, r.season] > 1
            return (not waiting, len(rows[rid]))

        budget = 6 if network else 0
        for rid in sorted(rows, key=priority):
            pages = rows[rid]
            for page in pages:
                key = episode_hash(page)
                if (
                    budget
                    and key
                    and key not in self.pages
                    and self.unlisted_until.get(key, 0) <= time.monotonic()
                    and self.retry_at.get(key, 0) <= time.monotonic()
                ):
                    budget -= 1
                    await self.resolve(page)
            if rid is None:
                continue
            ids = {
                self.pages.get(key)
                for page in pages
                if self.unlisted_until.get(key := episode_hash(page), 0) <= time.monotonic()
            }
            known = ids - {None}
            if len(known) > 1:
                await self.bind(rules[rid], 0)  # Ambiguous legacy association.
            elif len(ids) == 1 and known:
                await self.bind(rules[rid], next(iter(known)))

        self.pending_pages = {
            p
            for p in self.pending_pages
            if episode_hash(p) not in self.pages
            and self.unlisted_until.get(episode_hash(p), 0) <= time.monotonic()
        }
        from module.database.bangumi import _normalize_group_name

        anchors = defaultdict(list)
        for c in retained:
            rule = rules.get(c["rule_id"])
            key = episode_hash(release_page(c["url"], c.get("homepage")))
            if (
                rule
                and c.get("episode_key")
                and c.get("episode") is not None
                and self.pages.get(key)
                and self.pages[key] == self.identity(rule)
                and self.owners.get(c["url"]) == rule.id
            ):
                anchors[rule.id, _normalize_group_name(c.get("group")), self.pages[key]].append(
                    (c["episode"], c.get("episode_end") or c["episode"])
                )
        self.catalog, self.anchors = dict(rules), anchors

    def match_unlisted(self, torrent, rules):
        from module.database.bangumi import _all_title_patterns, _normalize_group_name
        from module.parser.analyser.tokenizer.result import MediaType, ReleaseKind

        _, release = parse_review_release(torrent.name)
        pack = bounded_batch(torrent.name, release)
        if not release or (not pack and release.release_kind is not ReleaseKind.SINGLE):
            return None
        release = pack or release
        if (
            release.media_type is not MediaType.EPISODE
            or release.episode is None
            or not release.group
        ):
            return None
        # Some parsers include trailing packaging notes in the parsed title.
        # Also compare whole multilingual titles before the first metadata bracket.
        label = re.match(r"^\s*[\[【][^\]】]+[\]】]\s*([^\[【]*)", torrent.name)
        titles = {release.title_en, release.title_zh, release.title_jp}
        if label:
            titles.update(label[1].split("/"))
        titles = {value.strip().casefold() for value in titles if value and value.strip()}
        group = _normalize_group_name(release.group)
        supplied = {r.id: r for r in rules}
        matches = [
            r
            for r in (self.catalog | supplied).values()
            if not r.deleted
            and r.episode_type == "episode"
            and (release.season is None or r.season == release.season)
            and group
            and _normalize_group_name(r.group_name) == group
            and any(
                t and t.strip().casefold() in titles
                for t in [r.official_title, *_all_title_patterns(r)]
            )
        ]
        if len(matches) != 1:
            return None
        rule = matches[0]
        identity = self.identity(rule)
        if rule.id not in supplied or rule.archived or not identity:
            return None
        if release.season is None:
            spans = self.anchors.get((rule.id, group, identity), ())
            if not any(first <= release.episode <= last + 1 for first, last in spans):
                return None
        return rule

    def members(self, series, rules):
        def identifies(rule):
            known = self.identity(rule)
            if known is not None:
                return known == series
            # Accept a classified release into its dedicated single-series rule
            # even before any torrent history exists. Keep persisted dedup keys
            # unchanged until observe() verifies the rule's history.
            ids = {
                sid
                for url in (getattr(rule, "rss_link", "") or "").split(",")
                if (sid := feed_series(url))
            }
            return ids == {series}

        return [r for r in rules if not r.deleted and identifies(r)]

    async def defer_creation(self, db, rules, item, torrent, series):
        from module.database.bangumi import _all_title_patterns
        from module.models import Torrent
        from sqlmodel import col, select

        # Discovery is feed-scoped, but a release already owned by AB is not new.
        owner = await db.session.scalar(
            select(Torrent.bangumi_id)
            .where(Torrent.url == torrent.url, col(Torrent.bangumi_id).is_not(None))
            .limit(1)
        )
        if owner is not None:
            return True
        for rule in rules:
            if rule.episode_type != item.episode_type or rule.season != item.season:
                continue
            identity = self.identity(rule)
            if rule.deleted and identity == series:
                return True
            if identity not in (None, 0):
                continue
            if rule.official_title == item.official_title:
                return True
            if item.title_raw and item.title_raw in _all_title_patterns(rule):
                # An exact alias is reason to verify the legacy releases, not to
                # bind two series. Non-Mikan records supply no evidence to verify.
                history = await db.torrent.search_by_bangumi_id(rule.id)
                if any(release_page(t.url, t.homepage) for t in history) or any(
                    release_page(c["url"], c.get("homepage"))
                    for c in await self.store.candidates(rule.id)
                ):
                    return True
        return False

    def match(self, torrent, rules, match_title, file_ranges):
        from module.database.bangumi import _release_matches_bangumi
        from module.parser.analyser.tokenizer.result import ReleaseKind

        page = release_page(torrent.url, torrent.homepage)
        if not page:
            return match_title(torrent.name, rules)
        key = episode_hash(page)
        series = self.pages.get(key)
        if self.unlisted_until.get(key, 0) > time.monotonic():
            return self.match_unlisted(torrent, rules)
        if series is None:
            self.pending_pages.add(page)
        owner = self.owners.get(torrent.url)
        if not series:
            return match_title(torrent.name, [r for r in rules if r.id == owner])
        members = self.members(series, rules)
        if not members:
            # Legacy records keep their explicit host association during backfill.
            return match_title(
                torrent.name, [r for r in rules if r.id == owner and self.identity(r) is None]
            )
        _, release = parse_review_release(torrent.name)
        if not release:
            return None
        batch = bounded_batch(torrent.name, release)
        members = [
            r for r in members if not r.archived and _release_matches_bangumi(batch or release, r)
        ]
        if not batch and release.release_kind is not ReleaseKind.SINGLE:
            members = [r for r in members if (r.id, torrent.url, torrent.name) in file_ranges]
        if owner and self.rules.get(owner, (None, None))[1] == series:
            return next((r for r in members if r.id == owner), None)
        return members[0] if len(members) == 1 else match_title(torrent.name, members)

    async def discover(self, analyser, rss, engine, torrent, series):
        from module.database.bangumi import (
            _all_title_patterns,
            _get_aliases_list,
            _set_aliases_list,
        )
        from module.models import Bangumi
        from module.parser.analyser.tokenizer.result import ReleaseKind

        normalized, release = parse_review_release(torrent.name)
        pack = bounded_batch(torrent.name, release)
        if not release or (not pack and release.release_kind is not ReleaseKind.SINGLE):
            return False
        # Host enrichment still supplies the display title, poster and routing.
        if pack:
            item = await project_batch(analyser, rss, torrent, pack)
        else:
            item = await analyser.torrent_to_data(
                torrent.model_copy(update={"name": normalized}), rss
            )
        if not isinstance(item, Bangumi):
            return False
        rules = await engine.db.bangumi.search_all()
        members = [r for r in self.members(series, rules) if r.episode_type == item.episode_type]
        if len(members) > 1 or (members and members[0].archived):
            return True  # Existing separate rules remain available for explicit consolidation.
        if members:
            rule = members[0]
            if item.title_raw and item.title_raw not in _all_title_patterns(rule):
                _set_aliases_list(rule, [*_get_aliases_list(rule), item.title_raw])
            if rss.url not in (rule.rss_link or "").split(","):
                rule.rss_link = ",".join(filter(None, (rule.rss_link, rss.url)))
        else:
            if await self.defer_creation(engine.db, rules, item, torrent, series):
                return True
            rule = item
        engine.db.add(rule)
        await engine.db.session.flush()
        await self.bind(rule, series)
        await engine.db.commit()
        return True
