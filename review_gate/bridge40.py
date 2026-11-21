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

from .audit import Audit, traced

PROVIDER = "subscription-review-qbittorrent"


class Bridge40:
    def __init__(self, store, log):
        self.store, self.log = store, log
        self.audit = Audit(log)
        self.lock = asyncio.Lock()
        self.replay_lock = asyncio.Lock()
        self.active = False
        self.permit = contextvars.ContextVar("subscription_review_permit", default=None)

    async def setup(self):
        from ab_sdk.downloader import AddResult, DownloaderCapabilities
        from module.conf import VERSION
        from module.downloader.client.qb_downloader import QbDownloader
        from module.downloader.download_client import DownloadClient
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
        if list(inspect.signature(self.original_filters).parameters) != [
            "self",
            "torrents",
            "matches",
        ]:
            raise RuntimeError("AB 批次过滤接口已变化；拒绝安装适配层")
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
            selected = bridge.match_rule(torrent.name, rules)
            return bridge.original_match(engine, torrent, [selected]) if selected else None

        self.match_wrapper = reviewed_match
        RSSEngine.match_torrent = reviewed_match
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
                for item in items:
                    cid = await bridge.capture(bangumi, item)
                    candidate = await bridge.store.candidate(cid)
                    reason = await bridge.store.decision(candidate)
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
                    reason = await bridge.store.decision(candidate)
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
        # Already-created GuardedQb instances retain active=False and refuse adds.

    @staticmethod
    def match_rule(name, rules):
        """Preserve host title/type matching, prefer exact groups, never guess a season."""
        from module.database.bangumi import (
            _all_title_patterns,
            _groups_are_similar,
            _normalize_group_name,
            match_bangumi_in_list,
        )
        from module.parser.analyser.selector import parse_configured_release_title

        release = parse_configured_release_title(name)
        title = release and (release.title_en or release.title_zh or release.title_jp)
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
            if not length or not match_bangumi_in_list(name, [rule]):
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
        from module.parser.analyser.selector import parse_configured_release_title

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
                    release = parse_configured_release_title(torrent.name)
                    if (
                        not target
                        or target.id == old.id
                        or target.archived
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
                        candidate, self.rule_data(target), self.parsed_data(target, torrent.name)
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
            return self.rule_data(rule) if rule is not None and not rule.deleted else None

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
            (self.rule_data(rule), self.torrent_data(t), self.parsed_data(rule, t.name), True)
            for t, rule in rows
        ]
        if entries:
            await self.store.import_history([], entries)
        return {p["episode_key"] for _, _, p, _ in entries if p["episode_key"]}

    @staticmethod
    def torrent_data(torrent):
        return {k: getattr(torrent, k, None) for k in ("name", "url", "homepage", "rss_id")}

    @staticmethod
    def parsed_data(rule, name):
        from module.manager.revision_policy import _adjust_episode
        from module.parser.analyser.selector import parse_configured_release_title
        from module.parser.release_policy import preference_identity, preference_revision

        release = parse_configured_release_title(name)
        # AB 4.0 tokenizer can read the season in "Euphonium 3 [13]" as
        # episode 3. Accept classic's correction only when an unambiguous
        # numeric episode bracket independently agrees (never guess a batch).
        brackets = re.findall(r"\[(\d{1,3}(?:\.5)?)(?:v\d+)?\]", name, re.I)
        if release and len(brackets) == 1 and release.episode != float(brackets[0]):
            from module.parser.analyser.tokenizer import classic

            alternate = classic.parse_release_title(name)
            if (
                alternate
                and alternate.episode == float(brackets[0])
                and alternate.group == release.group
                and alternate.season == release.season
                and not release.is_mixed_collection
            ):
                release = alternate
        identity = preference_identity(release, default_season=rule.season) if release else None
        fields = {
            k: getattr(release, k, None)
            for k in ("group", "resolution", "source", "subtitle", "episode")
        }
        # Retain an explicit leading release label that AB's known-group list
        # cannot classify (e.g. 【今晚月色真美】[title]). Do not change episode parsing.
        label = re.match(r"^【([^】]{1,80})】(?=\[)", name)
        if release and not fields["group"] and label:
            fields["group"] = label[1]
        fields["episode_key"] = None
        fields["revision"] = preference_revision(release) if release else 1
        bracket_revision = re.search(r"\[(\d{1,3}(?:\.5)?)v(\d+)\]", name, re.I)
        if identity and bracket_revision and float(bracket_revision[1]) == identity[2]:
            fields["revision"] = max(fields["revision"], int(bracket_revision[2]))
        if identity:
            # Include official title + mapped season/type/episode, shared across
            # rules, so a different group cannot re-download an existing episode.
            fields["episode_key"] = json.dumps(
                [
                    rule.official_title.casefold().strip(),
                    str(identity[0]),
                    identity[1] + (getattr(rule, "season_offset", 0) or 0),
                    _adjust_episode(identity[2], getattr(rule, "episode_offset", 0) or 0),
                ],
                ensure_ascii=False,
            )
        return fields

    async def capture(self, rule, torrent):
        if not rule.id:
            raise ValueError("番剧规则尚未入库")
        return await self.store.capture(
            self.rule_data(rule), self.torrent_data(torrent), self.parsed_data(rule, torrent.name)
        )

    async def filter(self, torrent, bangumi):
        from module.database import Database

        async with Database() as db:
            rule = await db.bangumi.search_id(bangumi.id)
        if not rule or rule.deleted:
            return False
        async with self.lock:
            cid = await self.capture(rule, torrent)
            return await self.store.decision(await self.store.candidate(cid)) is None

    async def sync_history(self):
        from module.database import Database

        from .policy import fingerprint

        await self.repair_associations()
        async with Database() as db:
            rules = await db.bangumi.search_all()
            history = await db.torrent.search_all()
        by_id = {r.id: r for r in rules if not r.deleted}
        async with self.lock:
            entries = [
                (
                    self.rule_data(by_id[t.bangumi_id]),
                    self.torrent_data(t),
                    self.parsed_data(by_id[t.bangumi_id], t.name),
                    t.downloaded,
                )
                for t in history
                if t.bangumi_id in by_id
            ]
            await self.store.import_history([self.rule_data(r) for r in by_id.values()], entries)
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
                            **self.parsed_data(rule, c["name"]),
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
        return Maintenance(engine.url.database, self.store.path, self.parsed_data)

    async def orphan_records(self):
        from module.conf import settings
        from module.database import Database
        from module.database.bangumi import match_bangumi_in_list
        from module.rss.engine import RSSEngine

        candidates = await self.store.candidates()
        known = {c["url"]: c for c in candidates}
        rows = []
        async with Database() as db:
            rules = await db.bangumi.search_all()
            engine = RSSEngine(db)
            for t in await db.torrent.search_orphans():
                rule = engine.match_torrent(t, rules)
                excluded = any(re.search(p, t.name) for p in settings.rss_parser.filter)
                if excluded:
                    reason, rule = "被 AB 全局过滤条件排除", None
                elif rule:
                    reason = (
                        "审核暂缓留下的未关联记录" if t.url in known else "可关联到现有番剧规则"
                    )
                elif match_bangumi_in_list(t.name, rules):
                    reason = "被单番过滤条件排除"
                else:
                    reason = "没有匹配的有效番剧规则；需先在 AB 修正标题或添加规则"
                rows.append(
                    {
                        "id": t.id,
                        "name": t.name,
                        "downloaded": t.downloaded,
                        "rule_id": rule.id if rule else None,
                        "rule_title": rule.official_title if rule else None,
                        "reason": reason,
                        "known": t.url in known,
                    }
                )
            # match_torrent changes in-memory objects; this diagnostic is read-only.
            await db.rollback()
        return rows

    async def repair_orphans(self, ids=None):
        from module.conf import settings
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
                    if torrent.downloaded and torrent.url not in known:
                        continue
                    if any(re.search(p, torrent.name) for p in settings.rss_parser.filter):
                        continue
                    rule = engine.match_torrent(torrent, rules)
                    if not rule or rule.archived:
                        continue
                    await self.capture(rule, torrent)
                    if torrent.downloaded:
                        await self.store.seed(
                            self.rule_data(rule),
                            self.torrent_data(torrent),
                            self.parsed_data(rule, torrent.name),
                        )
                    if ids is not None or torrent.url not in known:
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
            await self.sync_history()
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
        from module.conf import settings
        from module.database import Database
        from module.downloader.download_client import DownloadClient, resolve_downloader_id
        from module.models import Torrent
        from module.rss.engine import RSSEngine

        from .policy import fingerprint

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
            if not repair and await self.store.decision(candidate):
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
                if not repair and await self.store.decision(candidate):
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
                if any(re.search(p, torrent.name) for p in settings.rss_parser.filter):
                    continue
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
