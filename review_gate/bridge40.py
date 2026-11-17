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
import re

PROVIDER = "subscription-review-qbittorrent"


class Bridge40:
    def __init__(self, store, log):
        self.store, self.log = store, log
        self.lock = asyncio.Lock()
        self.active = False
        self.permit = contextvars.ContextVar("subscription_review_permit", default=None)

    async def setup(self):
        from ab_sdk.downloader import AddResult, DownloaderCapabilities
        from module.conf import VERSION
        from module.downloader.client.qb_downloader import QbDownloader
        from module.downloader.download_client import DownloadClient

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
        bridge = self

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

        self.client_factory = GuardedQb

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
                    except Exception:
                        await bridge.store.mark(candidate["id"], "retry", "投递异常；稍后重试")
                        raise
                    finally:
                        bridge.permit.reset(grant)
                    if outcome is AddResult.FAILED:
                        await bridge.store.mark(candidate["id"], "retry", "下载器未确认接收")
                        return outcome
                    await bridge.store.delivered(
                        candidate, instance_id=client.instance_id, save_path=bangumi.save_path
                    )
                    result = outcome
                return result

        guarded_add._subscription_review = True
        self.wrapper = guarded_add
        self.active = True
        DownloadClient.add_torrent = guarded_add

    async def teardown(self):
        async with self.lock:
            self.active = False
            if getattr(self, "facade", None) and self.facade.add_torrent is self.wrapper:
                self.facade.add_torrent = self.original
        # Already-created GuardedQb instances retain active=False and refuse adds.

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

    @staticmethod
    def torrent_data(torrent):
        return {k: getattr(torrent, k, None) for k in ("name", "url", "homepage", "rss_id")}

    @staticmethod
    def parsed_data(rule, name):
        from module.manager.revision_policy import _adjust_episode
        from module.parser.analyser.selector import parse_configured_release_title
        from module.parser.release_policy import preference_identity

        release = parse_configured_release_title(name)
        identity = preference_identity(release, default_season=rule.season) if release else None
        fields = {
            k: getattr(release, k, None)
            for k in ("group", "resolution", "source", "subtitle", "episode")
        }
        fields["episode_key"] = None
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

    async def replay(self):
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
            except Exception:
                self.log.exception("审核队列投递失败；保留候选等待重试")
        return {"sent": sent, "held": held}
