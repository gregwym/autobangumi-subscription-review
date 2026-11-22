"""Group reviewed releases by the series link on Mikan's public episode page."""

import asyncio
import re
import time
from collections import defaultdict

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


def page_series(data):
    soup = BeautifulSoup(data, "html.parser")
    ids = {
        int(match[1])
        for link in soup.select("p.bangumi-title a[href]")
        if (match := SERIES_PATH.fullmatch(link["href"]))
    }
    return ids.pop() if len(ids) == 1 else None


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
    return await asyncio.to_thread(page_series, bytes(body))


class MikanSeries:
    def __init__(self, store, rule_data):
        self.store, self.rule_data = store, rule_data
        self.pages = {}
        self.rules = {}
        self.owners = {}
        self.retry_at = {}
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
        try:
            series = await fetch_series(homepage)
        except httpx2.HTTPError, TimeoutError, ValueError:
            series = None
        if series is None:
            self.retry_at[key] = time.monotonic() + 600
            return None
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

    async def observe(self, rules, history, network):
        rows = defaultdict(list)
        owners = defaultdict(set)
        for t in history:
            if t.bangumi_id in rules or t.bangumi_id is None:
                owners[t.url].add(t.bangumi_id)
                rows[t.bangumi_id].append(t)
        self.owners = {url: next(iter(ids)) for url, ids in owners.items() if len(ids) == 1}
        budget = 6 if network else 0
        # Resolve small rules first so one long-running series cannot starve new rules.
        for rid, releases in sorted(rows.items(), key=lambda item: len(item[1])):
            for t in releases:
                page = release_page(t.url, t.homepage)
                key = episode_hash(page)
                if (
                    budget
                    and key
                    and key not in self.pages
                    and self.retry_at.get(key, 0) <= time.monotonic()
                ):
                    budget -= 1
                    await self.resolve(page)
            if rid is None:
                continue
            ids = {self.pages.get(episode_hash(release_page(t.url, t.homepage))) for t in releases}
            known = ids - {None}
            if len(known) > 1:
                await self.bind(rules[rid], 0)  # Ambiguous legacy association.
            elif len(ids) == 1 and known:
                await self.bind(rules[rid], next(iter(known)))

    def members(self, series, rules):
        return [r for r in rules if not r.deleted and self.identity(r) == series]

    def match(self, torrent, rules, match_title, file_ranges):
        from module.database.bangumi import _release_matches_bangumi
        from module.parser.analyser.tokenizer.result import ReleaseKind

        page = release_page(torrent.url, torrent.homepage)
        if not page:
            return match_title(torrent.name, rules)
        series = self.pages.get(episode_hash(page))
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
            # Do not resurrect deleted rules, or shadow an unresolved legacy rule.
            if any(
                r.official_title == item.official_title
                and r.episode_type == item.episode_type
                and r.season == item.season
                and (self.identity(r) in (None, 0) or (r.deleted and self.identity(r) == series))
                for r in rules
            ):
                return True
            rule = item
        engine.db.add(rule)
        await engine.db.session.flush()
        await self.bind(rule, series)
        await engine.db.commit()
        return True
