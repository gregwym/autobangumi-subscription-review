"""Explicit private-API adapter for AB 4.0.0; no host files are modified.

The facade is wrapped only in memory. A separate downloader provider is the
mandatory second gate: without this plugin/provider, AB cannot instantiate it.
All AB imports are isolated here so a future upstream SDK can replace this file.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import logging
import re
import time
from dataclasses import replace

from .audit import Audit, traced
from .batch import (
    batch_matches,
    bounded_batch,
    discover_batches,
    global_excluded,
    parse_review_release,
    parsed_batch,
    project_batch,
)
from .feature import confirmed_features, dedicated_feed
from .file_batch import FileBatches, numbered_title_aliases
from .mikan_series import TORRENT, MikanSeries, episode_hash, release_page
from .policy import fingerprint
from .versions import version_traits

PROVIDER = "subscription-review-qbittorrent"


class Bridge40:
    def __init__(self, store, log):
        self.store, self.log = store, log
        self.audit = Audit(log)
        self.lock = asyncio.Lock()
        self.replay_lock = asyncio.Lock()
        self.active = False
        self.features = {}
        self.file_batches = FileBatches(store)
        self.file_ranges = {}
        self.mikan = MikanSeries(store, self.rule_data)
        self.permit = contextvars.ContextVar("subscription_review_permit", default=None)

    async def setup(self):
        from ab_sdk.downloader import AddResult, DownloaderCapabilities
        from module.conf import VERSION
        from module.downloader.client.qb_downloader import QbDownloader
        from module.downloader.download_client import DownloadClient
        from module.models import ResponseModel
        from module.parser.analyser.selector import parser_engine_snapshot
        from module.parser.analyser.tokenizer.result import ReleaseKind
        from module.rss.analyser import RSSAnalyser
        from module.rss.engine import RSSEngine

        if VERSION != "4.0.0":
            raise RuntimeError(f"此兼容层仅支持 AB 4.0.0，当前为 {VERSION}")
        if list(inspect.signature(DownloadClient.add_torrent).parameters) != [
            "self",
            "torrent",
            "bangumi",
        ]:
            raise RuntimeError("AB 下载接口已变化；拒绝安装适配层")
        if getattr(DownloadClient.add_torrent, "_subscription_review", False):
            raise RuntimeError("审核适配层已安装；请重启 AB")
        self.facade, self.original = DownloadClient, DownloadClient.add_torrent
        self.engine_class = RSSEngine
        self.original_filters = RSSEngine._apply_plugin_filters
        self.original_match = RSSEngine.match_torrent
        self.analyser_class = RSSAnalyser
        self.original_discovery = RSSAnalyser.rss_to_data
        self.original_link = RSSAnalyser.link_to_data
        if list(inspect.signature(self.original_filters).parameters) != [
            "self",
            "torrents",
            "matches",
        ]:
            raise RuntimeError("AB 批次过滤接口已变化；拒绝安装适配层")
        await self.mikan.load()
        bridge = self

        @traced("rss.capture", quiet=True, audit=bridge.audit)
        async def reviewed_filters(engine, torrents, matches):
            # Capture the whole batch before choosing v1/v2, independent of RSS order.
            captured = []
            async with bridge.lock:
                for torrent, rule in zip(torrents, matches):
                    captured.append(await bridge.capture(rule, torrent) if rule else None)
            result = await bridge.original_filters(engine, torrents, matches)
            for torrent, rule, accepted, cid in zip(torrents, matches, result, captured):
                if rule and accepted is None and cid:
                    candidate = await bridge.store.candidate(cid)
                    if await bridge.store.decision(candidate):
                        # AB treats a deferred filter result as an orphan. Retain the
                        # association only; result stays None, so no download occurs.
                        torrent.bangumi_id = rule.id
            return result

        class GuardedQb(QbDownloader):
            capabilities = DownloaderCapabilities(
                can_query=True, can_rename=True, can_manage=True, can_rss_rules=False
            )

            def __init__(self, conn):
                super().__init__(conn.host, conn.username, conn.password, conn.ssl)
                self.review_instance_id = conn.instance_id

            async def add_torrents(
                self, torrent_urls, torrent_files, save_path, category, tags=None
            ):
                grant = bridge.permit.get()
                if (
                    not bridge.active
                    or not grant
                    or grant[:2] != (asyncio.current_task(), self.review_instance_id)
                    or grant[2] not in (tags or "").split(",")
                ):
                    bridge.log.error("订阅审核拒绝未经批准的投递")
                    return AddResult.FAILED
                # Consume the grant; accidental repeated calls must not reuse it.
                bridge.permit.set(None)
                return await super().add_torrents(
                    torrent_urls, torrent_files, save_path, category, tags
                )

            async def rss_set_rule(self, *args, **kwargs):
                raise RuntimeError("审核下载器不允许创建绕过审核的 qB RSS 规则")

        def reviewed_match(engine, torrent, rules):
            selected = bridge.select_rule(engine, torrent, rules)
            if selected:
                torrent.bangumi_id = selected.id
            return selected

        async def reviewed_discovery(analyser, rss, torrents, engine, bangumi_list):
            with parser_engine_snapshot():
                regular, batches, grouped = [], [], False
                deadline = asyncio.get_running_loop().time() + 8
                for torrent in await engine.db.torrent.check_new(torrents, rss=rss):
                    if global_excluded(torrent.name):
                        continue
                    page = release_page(torrent.url, torrent.homepage)
                    if page:
                        if (
                            episode_hash(page) not in bridge.mikan.pages
                            and asyncio.get_running_loop().time() >= deadline
                        ):
                            continue
                        series = await bridge.mikan.resolve(page)
                        if not series:
                            continue
                        if series:
                            async with bridge.mikan.discovery_lock:
                                handled = await bridge.mikan.discover(
                                    analyser, rss, engine, torrent, series
                                )
                            if handled:
                                grouped = True
                                continue
                    normalized, release = parse_review_release(torrent.name)
                    batch = bounded_batch(torrent.name, release)
                    if batch is not None:
                        batches.append((torrent, batch))
                    elif normalized == torrent.name:
                        regular.append(torrent)
                    elif release is not None and release.release_kind is ReleaseKind.SINGLE:
                        regular.append(torrent.model_copy(update={"name": normalized}))
                added = await bridge.original_discovery(
                    analyser, rss, regular, engine, bangumi_list
                )
                if batches:
                    rules = await engine.db.bangumi.search_all() if added else bangumi_list
                    added += await discover_batches(
                        analyser, rss, engine, batches, rules, bridge.match_rule
                    )
                return await engine.db.bangumi.search_all() if grouped else added

        async def reviewed_link(analyser, rss):
            with parser_engine_snapshot():
                torrents = await analyser.get_rss_torrents(rss.url)
                if not torrents:
                    return ResponseModel(
                        status=False,
                        status_code=406,
                        msg_en="Cannot find any torrent.",
                        msg_zh="无法找到种子。",
                    )
                batches = []
                for torrent in torrents:
                    if global_excluded(torrent.name):
                        continue
                    normalized, release = parse_review_release(torrent.name)
                    batch = bounded_batch(torrent.name, release)
                    if batch is not None:
                        batches.append((torrent, batch))
                    elif normalized == torrent.name or (
                        release is not None and release.release_kind is ReleaseKind.SINGLE
                    ):
                        for_parser = (
                            torrent
                            if normalized == torrent.name
                            else torrent.model_copy(update={"name": normalized})
                        )
                        data = await analyser.torrent_to_data(for_parser, rss)
                        if data:
                            return data
                if batches:
                    torrent, batch = batches[0]
                    return await project_batch(analyser, rss, torrent, batch)
            return ResponseModel(
                status=False,
                status_code=406,
                msg_en="Cannot parse this link.",
                msg_zh="无法解析此链接。",
            )

        self.match_wrapper = reviewed_match
        self.discovery_wrapper = reviewed_discovery
        self.link_wrapper = reviewed_link
        RSSEngine.match_torrent = reviewed_match
        RSSAnalyser.rss_to_data = reviewed_discovery
        RSSAnalyser.link_to_data = reviewed_link
        self.client_factory = GuardedQb

        @traced("download.dispatch", quiet=True, audit=bridge.audit)
        async def guarded_add(client, torrent, bangumi):
            from ab_sdk.downloader import AddResult

            if not bridge.active or client.instance.type != PROVIDER:
                bridge.log.error("下载被暂缓：实例必须使用订阅审核 qBittorrent 类型")
                return AddResult.FAILED
            # Hold one lock across approval checks and dispatch. Approval edits,
            # replay and RSS polling cannot race this critical section.
            async with bridge.lock:
                items = torrent if isinstance(torrent, list) else [torrent]
                if not items:
                    return AddResult.FAILED
                candidates = []
                exclusion = bridge.exclusion_check(bangumi.filter)
                for item in items:
                    if not await bridge.valid_dispatch_rule(item, bangumi):
                        return AddResult.FAILED
                    cid = await bridge.capture(bangumi, item)
                    candidate = await bridge.store.candidate(cid)
                    reason = await bridge.store.decision(candidate, exclusion=exclusion)
                    if reason not in (None, "已投递"):
                        bridge.audit.emit(
                            "download.held",
                            level=logging.DEBUG,
                            rule_id=bangumi.id,
                            candidate_id=cid,
                            reason=reason,
                        )
                        return AddResult.FAILED
                    candidates.append((item, candidate, reason))
                result = AddResult.DUPLICATE
                for item, candidate, reason in candidates:
                    # Re-evaluate after every send: a batch can contain two
                    # versions of one episode. The first success reserves it.
                    reason = await bridge.store.decision(candidate, exclusion=exclusion)
                    if reason == "已投递":
                        continue
                    if reason:
                        return AddResult.FAILED
                    await bridge.store.reserve(candidate)
                    grant = bridge.permit.set(
                        (asyncio.current_task(), client.instance_id, f"ab:{bangumi.id}")
                    )
                    try:
                        outcome = await bridge.original(client, item, bangumi)
                    except Exception as error:
                        await bridge.store.mark(candidate["id"], "retry", "投递异常；稍后重试")
                        bridge.audit.emit(
                            "download.retry",
                            level=logging.WARNING,
                            rule_id=bangumi.id,
                            candidate_id=candidate["id"],
                            error=type(error).__name__,
                            detail=str(error),
                        )
                        raise
                    finally:
                        bridge.permit.reset(grant)
                    if outcome is AddResult.FAILED:
                        await bridge.store.mark(candidate["id"], "retry", "下载器未确认接收")
                        bridge.audit.emit(
                            "download.retry",
                            level=logging.WARNING,
                            rule_id=bangumi.id,
                            candidate_id=candidate["id"],
                            detail="下载器未确认接收",
                        )
                        return outcome
                    await bridge.store.delivered(
                        candidate, instance_id=client.instance_id, save_path=bangumi.save_path
                    )
                    bridge.audit.emit(
                        "download.submitted",
                        rule_id=bangumi.id,
                        candidate_id=candidate["id"],
                        name=candidate["name"],
                        downloader=client.instance_id,
                        result=outcome.value,
                        detail="下载器已接受；尚非下载完成",
                    )
                    result = outcome
                return result

        guarded_add._subscription_review = True
        self.wrapper = guarded_add
        self.active = True
        DownloadClient.add_torrent = guarded_add
        self.filters_wrapper = reviewed_filters
        RSSEngine._apply_plugin_filters = reviewed_filters

    async def teardown(self):
        async with self.lock:
            self.active = False
            if getattr(self, "facade", None) and self.facade.add_torrent is self.wrapper:
                self.facade.add_torrent = self.original
            if (
                getattr(self, "engine_class", None)
                and self.engine_class._apply_plugin_filters is self.filters_wrapper
            ):
                self.engine_class._apply_plugin_filters = self.original_filters
            if (
                getattr(self, "engine_class", None)
                and self.engine_class.match_torrent is self.match_wrapper
            ):
                self.engine_class.match_torrent = self.original_match
            if getattr(self, "analyser_class", None):
                if self.analyser_class.rss_to_data is getattr(self, "discovery_wrapper", None):
                    self.analyser_class.rss_to_data = self.original_discovery
                if self.analyser_class.link_to_data is getattr(self, "link_wrapper", None):
                    self.analyser_class.link_to_data = self.original_link
        # Already-created GuardedQb instances retain active=False and refuse adds.

    async def valid_dispatch_rule(self, torrent, rule):
        """Recheck the full current catalog before using title-based Mikan routing."""
        from module.database import Database
        from module.rss import RSSEngine

        page = release_page(torrent.url, torrent.homepage)
        key = episode_hash(page)
        series = self.mikan.pages.get(key)
        binding = self.mikan.identity(rule)
        if series and binding is not None and binding != series:
            return False
        if self.mikan.unlisted_until.get(key, 0) > time.monotonic():
            async with Database() as db:
                rules = await db.bangumi.search_all()
            selected = self.select_rule(RSSEngine(None), torrent, rules)
            return selected is not None and selected.id == rule.id
        return (
            parsed_batch(torrent.name) is None
            or self.select_rule(RSSEngine(None), torrent, [rule]) is not None
        )

    def select_rule(self, engine, torrent, rules):
        selected = self.mikan.match(torrent, rules, self.match_rule, self.file_ranges)
        if not selected or global_excluded(torrent.name):
            return None
        if selected.filter and engine._get_filter_pattern(selected.filter).search(torrent.name):
            return None
        return selected

    @staticmethod
    def match_rule(name, rules, *, batch=None):
        """Preserve host title/type matching, prefer exact groups, never guess a season."""
        from module.database.bangumi import (
            _all_title_patterns,
            _groups_are_similar,
            _normalize_group_name,
            match_bangumi_in_list,
        )
        from module.parser.analyser.tokenizer.result import ReleaseKind

        normalized, release = parse_review_release(name) if batch is None else (name, batch)
        batch = batch or bounded_batch(name, release)
        if batch is None and (
            (release is not None and release.release_kind is not ReleaseKind.SINGLE)
            or (release is None and normalized != name)
        ):
            return None
        release = batch or release
        title = release and (release.title_en or release.title_zh or release.title_jp)
        if batch is not None:
            matching = [
                (rule, length) for rule, length in batch_matches(batch, rules) if not rule.deleted
            ]
            longest = max((length for _, length in matching), default=0)
            if len({rule.season for rule, length in matching if length == longest}) > 1:
                return None
            rules = [rule for rule, _ in matching if not rule.archived]
        ranked = []
        for rule in rules:
            if rule.deleted:
                continue
            # The host uses the same title predicate. Skip rules it cannot match
            # before calling a helper that re-parses the release on each call.
            length = max(
                (len(p) for p in _all_title_patterns(rule) if p in name or p == title),
                default=0,
            )
            if not length or (batch is None and not match_bangumi_in_list(normalized, [rule])):
                continue
            group = release.group if release else None
            exact = bool(
                group
                and rule.group_name
                and _normalize_group_name(group) == _normalize_group_name(rule.group_name)
            )
            rank = (length, int(exact), int(_groups_are_similar(group, rule.group_name)))
            ranked.append((rank, rule))
        if not ranked:
            return None
        best = max(rank for rank, _ in ranked)
        finalists = [r for rank, r in ranked if rank == best]
        if len({r.season for r in finalists}) > 1:
            return None
        return finalists[0]

    async def repair_associations(self):
        """Repair only unsent plugin candidates with an unambiguous exact-group match."""
        from module.database import Database
        from module.database.bangumi import _normalize_group_name

        async with self.lock:
            known = {(c["rule_id"], c["url"]): c for c in await self.store.candidates()}
            backup = None
            repaired = []
            async with Database() as db:
                rules = await db.bangumi.search_all()
                by_id = {r.id: r for r in rules if not r.deleted}
                for torrent in await db.torrent.search_all():
                    candidate = known.get((torrent.bangumi_id, torrent.url))
                    old = by_id.get(torrent.bangumi_id)
                    if (
                        torrent.downloaded
                        or not old
                        or not candidate
                        or candidate["status"] != "held"
                    ):
                        continue
                    target = self.match_rule(torrent.name, rules)
                    _, release = parse_review_release(torrent.name)
                    if (
                        not target
                        or target.id == old.id
                        or (
                            self.mikan.identity(old)
                            and self.mikan.identity(target) != self.mikan.identity(old)
                        )
                        or target.archived
                        or self.exclusion_check(target.filter)(torrent.name)
                        or old.official_title != target.official_title
                        or old.season == target.season
                        or not release
                        or not release.group
                        or _normalize_group_name(release.group)
                        != _normalize_group_name(target.group_name)
                        or _normalize_group_name(release.group)
                        == _normalize_group_name(old.group_name)
                    ):
                        continue
                    if backup is None:
                        backup = await asyncio.to_thread(self.maintenance().backup)
                    await self.store.move_candidate(
                        candidate,
                        self.rule_data(target),
                        self.parse_candidate(target, torrent.name, torrent.url),
                    )
                    torrent.bangumi_id = target.id
                    db.add(torrent)
                    repaired.append(
                        {"torrent_id": torrent.id, "from_rule": old.id, "to_rule": target.id}
                    )
                await db.commit()
            if repaired:
                self.audit.emit("maintenance.associations", repaired=repaired, backup=str(backup))

    @staticmethod
    def rule_data(rule):
        fields = (
            "id",
            "official_title",
            "title_raw",
            "season",
            "episode_type",
            "group_name",
            "episode_offset",
            "season_offset",
        )
        return {k: getattr(rule, k, None) for k in fields}

    async def current_rule(self, rid):
        from module.database import Database

        async with Database() as db:
            rule = await db.bangumi.search_id(rid)
            return (
                (self.rule_data(rule), rule.filter)
                if rule is not None and not rule.deleted
                else None
            )

    @staticmethod
    def exclusion_check(rule_filter):
        from module.rss.engine import RSSEngine

        pattern = RSSEngine(None)._get_filter_pattern(rule_filter) if rule_filter else None

        def excluded(name):
            if global_excluded(name):
                return "被 AB 全局过滤条件排除"
            if pattern and pattern.search(name):
                return "被 AB 单番过滤条件排除"
            return None

        return excluded

    async def sync_show_downloads(self, title):
        """Refresh dedup for this title, including downloads from other rules."""
        from module.database import Database
        from module.models import Bangumi, Torrent
        from sqlmodel import col, select

        async with Database() as db:
            statement = (
                select(Torrent, Bangumi)
                .join(Bangumi, col(Bangumi.id) == col(Torrent.bangumi_id))
                .where(
                    Bangumi.official_title == title,
                    col(Bangumi.deleted).is_(False),
                    col(Torrent.downloaded).is_(True),
                )
            )
            rows = (await db.session.execute(statement)).all()
        entries = [
            (
                self.rule_data(rule),
                self.torrent_data(t),
                self.parse_candidate(rule, t.name, t.url),
                True,
            )
            for t, rule in rows
        ]
        if entries:
            await self.store.import_history([], entries)
        return {p["episode_key"] for _, _, p, _ in entries if p["episode_key"]}

    @staticmethod
    def torrent_data(torrent):
        return {k: getattr(torrent, k, None) for k in ("name", "url", "homepage", "rss_id")}

    @staticmethod
    def parsed_data(
        rule,
        name,
        *,
        feature=False,
        file_range=None,
        inspect_title=False,
        file_special=None,
        file_resolution=None,
    ):
        from module.manager.revision_policy import _adjust_episode
        from module.parser.analyser.tokenizer.result import MediaType, ReleaseKind
        from module.parser.release_policy import preference_identity, preference_revision

        normalized, release = parse_review_release(name)
        batch = bounded_batch(name, release)
        if file_range and file_range[0] == file_range[1] and release:
            release = replace(release, episode=file_range[0])
        elif file_range and release and batch is None:
            batch = replace(
                release,
                episode=file_range[0],
                episode_end=file_range[1],
                release_kind=ReleaseKind.RANGE,
                media_type=MediaType.EPISODE,
            )
        if batch is not None:
            release = batch
        # AB 4.0 tokenizer can read the season in "Euphonium 3 [13]" as
        # episode 3. Accept classic's correction only when an unambiguous
        # numeric episode bracket independently agrees (never guess a batch).
        brackets = re.findall(r"\[(\d{1,3}(?:\.5)?)(?:v\d+)?\]", name, re.I)
        if (
            release
            and release.release_kind is ReleaseKind.SINGLE
            and len(brackets) == 1
            and release.episode != float(brackets[0])
        ):
            from module.parser.analyser.tokenizer import classic

            alternate = classic.parse_release_title(normalized)
            if (
                alternate
                and alternate.release_kind is ReleaseKind.SINGLE
                and alternate.episode == float(brackets[0])
                and alternate.group == release.group
                and alternate.season == release.season
                and not release.is_mixed_collection
            ):
                release = alternate
        identity = preference_identity(release, default_season=rule.season) if release else None
        if (
            batch
            and (rule.episode_type == "episode" or (file_range and rule.episode_type == "special"))
            and (batch.season is None or batch.season == rule.season)
        ):
            identity = (batch.media_type, rule.season, batch.episode)
        uncertain = (
            inspect_title
            and not file_range
            and not batch
            and numbered_title_aliases(name, rule, release)
        )
        if uncertain:
            identity = None
        fields = {
            k: getattr(release, k, None)
            for k in ("group", "resolution", "source", "subtitle", "episode")
        }
        # Retain an explicit leading release label that AB's known-group list
        # cannot classify (e.g. 【今晚月色真美】[title]). Do not change episode parsing.
        label = re.match(r"^【([^】]{1,80})】(?=\[)", name)
        if release and not fields["group"] and label:
            fields["group"] = label[1]
        if uncertain:
            fields["episode"] = None
        fields["episode_key"] = None
        fields["episode_end"] = batch.episode_end if batch else None
        if (
            release
            and rule.episode_type == "movie"
            and release.media_type is MediaType.MOVIE
            and release.release_kind is ReleaseKind.SINGLE
            and release.episode is None
            and file_range is None
        ):
            feature = True
        fields["feature"] = feature
        fields["file_special"] = file_special
        fields["resolution"] = fields["resolution"] or file_resolution
        fields["revision"] = preference_revision(release) if release else 1
        bracket_revision = re.search(r"\[(\d{1,3}(?:\.5)?)v(\d+)\]", name, re.I)
        if identity and bracket_revision and float(bracket_revision[1]) == identity[2]:
            fields["revision"] = max(fields["revision"], int(bracket_revision[2]))
        if identity or feature:
            offset = getattr(rule, "episode_offset", 0) or 0
            fields["episode_key"] = json.dumps(
                [
                    rule.official_title.casefold().strip(),
                    str(identity[0]) if identity else "feature",
                    (identity[1] if identity else rule.season)
                    + (getattr(rule, "season_offset", 0) or 0),
                    _adjust_episode(identity[2], offset) if identity else None,
                    version_traits({**fields, "name": name})[0],
                    _adjust_episode(batch.episode_end, offset) if batch else None,
                ],
                ensure_ascii=False,
            )
        return fields

    def parse_candidate(self, rule, name, url):
        key = (rule.id, url, name)
        fp = fingerprint(self.rule_data(rule))
        feature = self.features.get(key) == fp
        proof = self.file_ranges.get(key)
        content = proof[1] if proof and proof[0] == fp else None
        data = self.parsed_data(
            rule,
            name,
            feature=feature or bool(content and content.special and content.span is None),
            file_range=content.span if content else None,
            file_special=(
                content.special
                if content and not (rule.episode_type == "movie" and content.special == "MOVIE")
                else None
            ),
            file_resolution=content.resolution if content else None,
            inspect_title=bool(TORRENT.fullmatch(url)),
        )

        series = self.mikan.identity(rule)
        if series and data["episode_key"]:
            identity = json.loads(data["episode_key"])
            data["episode_key"] = json.dumps([*identity, series], ensure_ascii=False)
        return data

    async def capture(self, rule, torrent, *, first_seen=None):
        if not rule.id:
            raise ValueError("番剧规则尚未入库")
        return await self.store.capture(
            self.rule_data(rule),
            self.torrent_data(torrent),
            self.parse_candidate(rule, torrent.name, torrent.url),
            first_seen=first_seen,
        )

    async def filter(self, torrent, bangumi):
        from module.database import Database

        async with Database() as db:
            rule = await db.bangumi.search_id(bangumi.id)
        if not rule or rule.deleted:
            return False
        if not await self.valid_dispatch_rule(torrent, rule):
            return False
        async with self.lock:
            cid = await self.capture(rule, torrent)
            return (
                await self.store.decision(
                    await self.store.candidate(cid), exclusion=self.exclusion_check(rule.filter)
                )
                is None
            )

    async def sync_history(self, *, inspect_files=False):
        from module.database import Database

        await self.repair_associations()
        async with Database() as db:
            rules = await db.bangumi.search_all()
            history = await db.torrent.search_all()
            sources = {rss.id: dedicated_feed(rss.url) for rss in await db.rss.search_all()}
        by_id = {r.id: r for r in rules if not r.deleted}
        retained = await self.store.candidates()
        await self.mikan.observe(by_id, history, inspect_files, retained)
        records = [(t.bangumi_id, self.torrent_data(t)) for t in history] + [
            (c["rule_id"], c) for c in retained
        ]
        identified = confirmed_features(by_id, records, sources)
        file_ranges = await self.file_batches.identify(
            by_id, records, sources, identified, network=inspect_files, mikan=self.mikan
        )
        async with self.lock:
            retained = await self.store.candidates()
            self.features = {key: fingerprint(self.rule_data(by_id[key[0]])) for key in identified}
            self.file_ranges = {
                key: (fingerprint(self.rule_data(by_id[key[0]])), span)
                for key, span in file_ranges.items()
            }
            entries = [
                (
                    self.rule_data(by_id[t.bangumi_id]),
                    self.torrent_data(t),
                    self.parse_candidate(by_id[t.bangumi_id], t.name, t.url),
                    t.downloaded,
                )
                for t in history
                if t.bangumi_id in by_id
            ]
            await self.store.import_history([self.rule_data(r) for r in by_id.values()], entries)
            shadows = []
            for c in retained:
                owner = self.mikan.owners.get(c["url"])
                key = episode_hash(release_page(c["url"], c.get("homepage")))
                series = self.mikan.pages.get(key)
                if (
                    c["status"] == "held"
                    and owner != c["rule_id"]
                    and owner in by_id
                    and c["rule_id"] in by_id
                    and series
                    and self.mikan.identity(by_id[owner])
                    == self.mikan.identity(by_id[c["rule_id"]])
                    == series
                ):
                    shadows.append((c, owner))
            await self.store.discard_shadows(shadows)
            # Refresh metadata of retained candidates too, including rows whose
            # original AB rule was deleted. No approval is granted here.
            from types import SimpleNamespace

            stored = {r["id"]: r for r in await self.store.rules()}
            updates = []
            for c in await self.store.candidates():
                rule = by_id.get(c["rule_id"]) or SimpleNamespace(**stored[c["rule_id"]])
                updates.append(
                    (
                        c["id"],
                        {
                            **(
                                self.parse_candidate(rule, c["name"], c["url"])
                                if rule.id in by_id
                                else self.parsed_data(
                                    rule, c["name"], feature=c.get("feature", False)
                                )
                            ),
                            "rule_fingerprint": fingerprint(self.rule_data(rule)),
                        },
                    )
                )
            await self.store.refresh_metadata(updates)
            self.audit.emit(
                "history.sync",
                level=logging.DEBUG,
                rules=len(by_id),
                history=len(history),
                candidates=len(updates),
                writes=["plugin rules/candidates/episode identities"],
                grants_approval=False,
            )

    async def health(self):
        from module.conf import VERSION, settings

        instances = [i for i in settings.plugins.instances if i.point == "downloader"]
        # Gate all configured downloaders, not just the default. No silently
        # unprotected per-rule or per-RSS override during migration.
        unsafe = [i.id for i in instances if i.provider != PROVIDER]
        return {
            "version": VERSION,
            "active": self.active,
            "ready": self.active and bool(instances) and not unsafe,
            "unguarded_instances": unsafe,
            "provider": PROVIDER,
        }

    async def activate(self, context):
        """Use AB's config reload choreography; never expose connection secrets."""
        from module.conf import settings
        from module.conf.config import CONFIG_PATH

        from .configuration import configure

        instances = [i for i in settings.plugins.instances if i.point == "downloader"]
        if not instances or any(i.provider not in {"qbittorrent", PROVIDER} for i in instances):
            raise ValueError("当前只支持 qBittorrent 下载器，请先检查下载设置")
        async with self.lock:
            result = await asyncio.to_thread(
                configure, CONFIG_PATH, [i.id for i in instances], True
            )
        # Reload may call teardown: release the bridge lock before entering it.
        # On failure the guarded provider stays on disk, never an unsafe fallback.
        await context.reload_settings()
        return {"enabled": True, "changed_instances": result["changed_instances"]}

    async def reconcile(self, candidate, rule, torrent, instance_id):
        """Repair AB's history after replay, including existing orphan rows.

        No deletion: same-URL entries are updated in place. qB duplicates count
        as success, covering a crash between network submission and DB commit.
        """
        from module.database import Database
        from module.models import Torrent
        from sqlmodel import select

        async with Database() as db:
            rows = (
                (await db.session.execute(select(Torrent).where(Torrent.url == candidate["url"])))
                .scalars()
                .all()
            )
            if not rows:
                rows = [torrent]
            for row in rows:
                if row.bangumi_id not in (None, rule.id):
                    continue
                row.bangumi_id = rule.id
                row.downloaded = True
                row.downloader_id = instance_id
                db.add(row)
            stored_rule = await db.bangumi.search_id(rule.id)
            if stored_rule and rule.save_path:
                stored_rule.save_path = rule.save_path
                db.add(stored_rule)
            await db.commit()

    def maintenance(self):
        from module.database.combine import async_session_factory

        from .maintenance import Maintenance

        engine = async_session_factory.kw["bind"]
        return Maintenance(engine.url.database, self.store.path, self.parse_candidate)

    async def orphan_records(self):
        from module.database import Database

        candidates = await self.store.candidates()
        known = {c["url"] for c in candidates}
        rows = []
        async with Database() as db:
            rules = await db.bangumi.search_all()
            for t in await db.torrent.search_orphans():
                rule = self.match_rule(t.name, rules)
                excluded = self.exclusion_check(rule.filter if rule else None)(t.name)
                repairable = False
                if excluded:
                    reason = excluded
                elif not rule:
                    reason = "没有匹配的有效番剧规则；需先在 AB 修正标题或添加规则"
                elif rule.archived:
                    reason = "匹配的番剧已归档"
                elif t.downloaded and t.url not in known and parsed_batch(t.name) is None:
                    reason = "已有下载但缺少可确认的历史关联"
                else:
                    reason = (
                        "审核暂缓留下的未关联记录" if t.url in known else "可关联到现有番剧规则"
                    )
                    repairable = True
                rows.append(
                    {
                        "id": t.id,
                        "name": t.name,
                        "downloaded": t.downloaded,
                        "rule_id": rule.id if rule else None,
                        "rule_title": rule.official_title if rule else None,
                        "reason": reason,
                        "repairable": repairable,
                        "known": t.url in known,
                    }
                )
        return rows

    async def repair_orphans(self, ids=None):
        from module.database import Database
        from module.rss.engine import RSSEngine

        fixed, merged, backup = [], [], None
        async with self.lock:
            known = {c["url"] for c in await self.store.candidates()}
            async with Database() as db:
                await db.begin_write()
                rules = await db.bangumi.search_all()
                active = {r.id for r in rules if not r.deleted}
                downloads_by_url = {}
                for row in await db.torrent.search_all():
                    if row.downloaded and row.bangumi_id in active:
                        downloads_by_url.setdefault(row.url, row)
                engine = RSSEngine(db)
                for torrent in await db.torrent.search_orphans():
                    if ids is not None and torrent.id not in ids:
                        continue
                    duplicate = downloads_by_url.get(torrent.url)
                    if duplicate and not torrent.downloaded:
                        if backup is None:
                            backup = str(await asyncio.to_thread(self.maintenance().backup))
                        if duplicate.rss_id is None:
                            duplicate.rss_id = torrent.rss_id
                            db.add(duplicate)
                        await db.session.delete(torrent)
                        merged.append({"removed": torrent.id, "kept": duplicate.id})
                        continue
                    if (
                        torrent.downloaded
                        and torrent.url not in known
                        and parsed_batch(torrent.name) is None
                    ):
                        continue
                    rule = engine.match_torrent(torrent, rules)
                    if not rule or rule.archived:
                        continue
                    # Already persisted by AB. If the plugin has no original
                    # observation time, don't treat it as new for future-only rules.
                    await self.capture(rule, torrent, first_seen=0)
                    if torrent.downloaded:
                        await self.store.seed(
                            self.rule_data(rule),
                            self.torrent_data(torrent),
                            self.parse_candidate(rule, torrent.name, torrent.url),
                        )
                    if ids is not None:
                        await self.store.hold(rule.id)
                    db.add(torrent)
                    fixed.append(torrent.id)
                await db.commit()
        if fixed or merged:
            self.audit.emit(
                "maintenance.orphans",
                repaired=fixed,
                merged=merged,
                backup=backup,
                writes=["AB torrent associations", "plugin candidates"],
            )
        return {"repaired": fixed, "merged": merged, "backup": backup}

    @traced("maintenance.queue", quiet=True)
    async def replay(self):
        async with self.replay_lock:
            await self.sync_history(inspect_files=True)
            repairs = await self.repair_orphans()
            result = await self._replay()
            self.audit.periodic(
                "maintenance.queue",
                **result,
                repaired=len(repairs["repaired"]),
                merged=len(repairs["merged"]),
                reads=["AB rules/history", "plugin candidates/policies/dedup"],
                writes=["plugin history/metadata cache"],
            )
            return result

    async def _replay(self):
        from ab_sdk.downloader import AddResult
        from module.database import Database
        from module.downloader.download_client import DownloadClient, resolve_downloader_id
        from module.models import Torrent
        from module.rss.engine import RSSEngine

        if not (await self.health())["ready"]:
            return {"sent": 0, "held": 0, "error": "先将下载器类型切换为订阅审核 qBittorrent"}
        sent, held = 0, 0
        # Bound each tick, while retaining every pending candidate on disk.
        for candidate in await self.store.candidates():
            if sent >= 30:
                break
            if candidate["status"] == "delivered":
                continue
            repair = candidate["status"] == "sent"
            if not repair and await self.store.decision(candidate, scan_revisions=False):
                held += 1
                continue
            async with Database() as db:
                rule = await db.bangumi.search_id(candidate["rule_id"])
                if not rule or rule.deleted or rule.archived:
                    continue
                if candidate.get("rule_fingerprint") != fingerprint(self.rule_data(rule)):
                    await self.store.register(self.rule_data(rule))
                    continue
                await self.store.register(self.rule_data(rule))
                if not repair and await self.store.decision(
                    candidate, exclusion=self.exclusion_check(rule.filter)
                ):
                    continue
                instance_id = resolve_downloader_id(rule.downloader_id)
                # Respect source-level routing too, as AB's regular refresh does.
                if not rule.downloader_id and candidate.get("rss_id"):
                    rss = await db.rss.search_id(candidate["rss_id"])
                    if rss:
                        instance_id = resolve_downloader_id(rule.downloader_id, rss.downloader_id)
                torrent = Torrent(
                    name=candidate["name"],
                    url=candidate["url"],
                    homepage=candidate.get("homepage"),
                    rss_id=candidate.get("rss_id"),
                )
                if repair:
                    instance_id = candidate.get("sent_downloader_id") or instance_id
                    rule.save_path = candidate.get("sent_save_path") or rule.save_path
                    await self.reconcile(candidate, rule, torrent, instance_id)
                    await self.store.mark(candidate["id"], "delivered")
                    self.audit.emit(
                        "download.receipt.reconciled", candidate_id=candidate["id"], rule_id=rule.id
                    )
                    continue
                # Replay is not permission to bypass host exclusions or plugins.
                engine = RSSEngine(db)
                if engine.match_torrent(torrent, [rule]) is None:
                    continue
                if not await engine.plugin_accepts(torrent, rule):
                    continue
            try:
                async with DownloadClient(instance_id) as client:
                    result = await client.add_torrent(torrent, rule)
                if result is not AddResult.FAILED:
                    await self.reconcile(candidate, rule, torrent, instance_id)
                    await self.store.mark(candidate["id"], "delivered")
                    sent += 1

            except Exception as error:
                self.audit.emit(
                    "download.retry",
                    error=type(error).__name__,
                    reason=str(error),
                    level=logging.WARNING,
                    rule_id=candidate["rule_id"],
                    candidate_id=candidate["id"],
                    detail="审核队列投递失败；保留候选等待重试",
                )
        return {"sent": sent, "held": held}
