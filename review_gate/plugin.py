from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from pathlib import Path

from ab_sdk import Plugin, Verdict, hook, points, provider
from ab_sdk.tasks import ScheduledTask
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .bridge40 import PROVIDER, Bridge40
from .policy import Policy
from .store import Store
from .updater import Updater
from .versions import variants


class UpdateRequest(BaseModel):
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$", max_length=40)


class ReviewRequest(BaseModel):
    include: list[str] = Field(default_factory=list, max_length=32)
    version_id: str | None = Field(default=None, max_length=40)
    exclude: list[str] = Field(default_factory=list, max_length=32)
    mode: str = "future"
    from_episode: float = Field(default=1, ge=0, le=100000)
    unfiltered: bool = False
    version_ids: list[str] = Field(default_factory=list, max_length=100)


class HistoryRequest(BaseModel):
    episodes: list[float] = Field(min_length=1, max_length=2000)


class OrphanRequest(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=2000)


class ApprovalRequest(BaseModel):
    preview_token: str = Field(min_length=20, max_length=100)


def snapshot(revision, candidates):
    rows = [(c["id"], c["name"], c["status"]) for c in candidates]
    return hashlib.sha256(json.dumps([revision, rows], sort_keys=True).encode()).hexdigest()


class SubscriptionReview(Plugin):
    async def setup(self):
        self.previews = {}
        self.history_previews = {}
        self.maintenance_lock = asyncio.Lock()
        self.store = Store(self.ctx.data_dir / "review.sqlite3")
        await self.store.init()
        self.bridge = Bridge40(self.store, self.ctx.log)
        self.updater = Updater(Path(__file__).resolve().parents[1], self.ctx.data_dir, self.bridge)
        await self.bridge.setup()
        try:
            await self.bridge.sync_history()
        except BaseException:
            await self.bridge.teardown()
            raise

    async def teardown(self):
        if hasattr(self, "bridge"):
            await self.bridge.teardown()

    @hook(points.TORRENT_FILTER, priority=1)
    async def review(self, torrent, release, bangumi):
        allowed = await self.bridge.filter(torrent, bangumi)
        return Verdict.ok() if allowed else Verdict.reject("订阅审核：等待批准或不符合版本条件")

    @provider(points.DOWNLOADER, id=PROVIDER)
    def downloader(self):
        return self.bridge.client_factory

    @provider(points.SCHEDULED_TASK, id="replay")
    def replay_task(self):
        return ScheduledTask(run=self.bridge.replay, interval=30, initial_delay=30)

    @provider(points.SCHEDULED_TASK, id="check-update")
    def update_task(self):
        return ScheduledTask(run=self.updater.check, interval=21600, initial_delay=60)

    @provider(points.API_ROUTER, id="api")
    def api(self):
        router = APIRouter()

        async def find_rule(rid):
            for r in await self.store.rules():
                if r["id"] == rid:
                    return r
            raise HTTPException(404, "未找到番剧规则")

        @router.get("/update")
        async def update_status():
            return self.updater.status()

        @router.post("/update/check")
        async def update_check():
            return await self.updater.check()

        @router.post("/update/install")
        async def update_install(body: UpdateRequest, request: Request):
            try:
                async with self.maintenance_lock:
                    return await self.updater.install(body.version, request.app.state.ctx)
            except ValueError as e:
                raise HTTPException(409, str(e)) from e
            except Exception as e:
                self.ctx.log.exception("插件更新失败")
                raise HTTPException(502, "更新服务暂不可用，旧版已保留；请稍后重试") from e

        @router.get("/rules")
        async def rules():
            return {"health": await self.bridge.health(), "rules": await self.store.rules()}

        @router.post("/activate")
        async def activate(request: Request):
            try:
                return await self.bridge.activate(request.app.state.ctx)
            except ValueError as e:
                raise HTTPException(409, str(e)) from e

        @router.get("/rules/{rid}/versions")
        async def versions(rid: int):
            await self.bridge.sync_history()
            await self.bridge.repair_orphans()
            rule = await find_rule(rid)
            items = await self.store.candidates(rid)
            return {"rule": rule, "versions": variants(items), "health": await self.bridge.health()}

        @router.get("/rules/{rid}/candidates")
        async def candidates(rid: int):
            await find_rule(rid)
            result = []
            for c in await self.store.candidates(rid):
                # Never expose signed RSS/torrent URLs in the UI response.
                result.append(
                    {
                        k: v
                        for k, v in c.items()
                        if k not in {"url", "homepage", "rss_id", "episode_key"}
                    }
                    | {"reason": await self.store.decision(c)}
                )
            return result

        @router.post("/rules/{rid}/preview")
        async def preview(rid: int, body: ReviewRequest):
            async with self.bridge.lock:
                rule = await find_rule(rid)
                items = await self.store.candidates(rid)
                data = body.model_dump(exclude={"version_id"})
                if body.unfiltered and (
                    body.version_id or body.version_ids or body.include or body.exclude
                ):
                    raise HTTPException(422, "不过滤版本不能同时设置版本或关键词")
                if body.version_ids:
                    if body.version_id or body.include:
                        raise HTTPException(422, "请选择多个版本或自定义关键词，不能同时设置")
                    available = {v["id"] for v in variants(items)}
                    if not set(body.version_ids) <= available:
                        raise HTTPException(409, "所选版本已变化，请刷新后重新选择")
                    data["version_ids"] = sorted(set(body.version_ids))
                if body.version_id:
                    selected = next(
                        (v for v in variants(items) if v["id"] == body.version_id), None
                    )
                    if selected is None:
                        raise HTTPException(409, "这个版本已变化，请刷新后重新选择")
                    if body.include:
                        raise HTTPException(422, "请选择版本或使用自定义条件，不能同时设置")
                    data["include"] = selected["include"]
                    data["version_id"] = selected["id"]
                try:
                    policy = Policy(**data, cutoff=time.time()).validate()
                except ValueError as e:
                    raise HTTPException(422, str(e)) from e
                token = secrets.token_urlsafe(32)
                # Expiring, bounded preview receipts prevent stale approvals.
                self.previews = {
                    k: v for k, v in self.previews.items() if v["expires"] > time.time()
                }
                if len(self.previews) >= 100:
                    self.previews.pop(next(iter(self.previews)))
                self.previews[token] = {
                    "rid": rid,
                    "policy": policy,
                    "revision": rule["revision"],
                    "snapshot": snapshot(rule["revision"], items),
                    "expires": time.time() + 600,
                }
                rows, episodes = [], set()
                for c in items:
                    reason = await self.store.decision(c, override=policy)
                    if reason is None and c["episode_key"] in episodes:
                        reason = "同集重复候选，保留先出现的匹配版本"
                    if reason is None:
                        episodes.add(c["episode_key"])
                    rows.append({"id": c["id"], "name": c["name"], "reason": reason})
                return {
                    "preview_token": token,
                    "items": rows,
                    "will_download": sum(r["reason"] is None for r in rows),
                    "policy": policy.dump(),
                }

        @router.post("/rules/{rid}/approve")
        async def approve(rid: int, body: ApprovalRequest):
            if not (await self.bridge.health())["ready"]:
                raise HTTPException(409, "下载器尚未受保护；请先按安装说明切换类型")
            async with self.bridge.lock:
                receipt = self.previews.pop(body.preview_token, None)
                if not receipt or receipt["rid"] != rid or receipt["expires"] < time.time():
                    raise HTTPException(409, "预览已失效，请重新预览")
                rule = await find_rule(rid)
                items = await self.store.candidates(rid)
                if snapshot(rule["revision"], items) != receipt["snapshot"]:
                    raise HTTPException(409, "候选或规则已变化，请重新预览")
                await self.store.set_policy(rid, receipt["policy"], receipt["revision"])
            return {"approved": True, "message": "已开始追番，后续新集将按此版本自动下载"}

        @router.post("/rules/{rid}/hold")
        async def hold(rid: int):
            async with self.bridge.lock:
                await find_rule(rid)
                await self.store.hold(rid)
            return {"held": True}

        @router.get("/maintenance/rules")
        async def maintenance_rules():
            return await asyncio.to_thread(self.bridge.maintenance().rules)

        @router.get("/rules/{rid}/history")
        async def history(rid: int):
            try:
                await self.bridge.sync_history()
                return await asyncio.to_thread(self.bridge.maintenance().history, rid)
            except ValueError as e:
                raise HTTPException(409, str(e)) from e

        @router.post("/rules/{rid}/history/preview")
        async def history_preview(rid: int, body: HistoryRequest):
            try:
                await self.bridge.sync_history()
                async with self.bridge.lock:
                    receipt = await asyncio.to_thread(
                        self.bridge.maintenance().preview, rid, body.episodes
                    )
                now = time.time()
                self.history_previews = {
                    k: v for k, v in self.history_previews.items() if v["expires"] > now
                }
                if len(self.history_previews) >= 100:
                    self.history_previews.pop(next(iter(self.history_previews)))
                token = secrets.token_urlsafe(32)
                self.history_previews[token] = {**receipt, "expires": now + 600}
                return {"preview_token": token, **receipt["summary"]}
            except ValueError as e:
                raise HTTPException(409, str(e)) from e

        @router.post("/rules/{rid}/history/clear")
        async def history_clear(rid: int, body: ApprovalRequest, request: Request):
            async with self.maintenance_lock:
                receipt = self.history_previews.pop(body.preview_token, None)
                if not receipt or receipt["rid"] != rid or receipt["expires"] < time.time():
                    raise HTTPException(409, "预览已失效，请重新预览")
                context = request.app.state.ctx
                running = context.is_running
                # Stop outside bridge locks so scheduled tasks can finish/cancel.
                if running:
                    await context.stop()
                try:
                    async with self.bridge.replay_lock, self.bridge.lock:
                        return await asyncio.to_thread(self.bridge.maintenance().clear, receipt)
                except ValueError as e:
                    raise HTTPException(409, str(e)) from e
                finally:
                    if running:
                        await context.start_tasks()

        @router.get("/maintenance/orphans")
        async def orphans():
            return await self.bridge.orphan_records()

        @router.post("/maintenance/orphans/repair")
        async def repair_orphans(body: OrphanRequest):
            async with self.bridge.replay_lock:
                return await self.bridge.repair_orphans(set(body.ids))

        @router.post("/sync")
        async def sync():
            await self.bridge.sync_history()
            return {"synced": True}

        @router.post("/replay")
        async def replay():
            return await self.bridge.replay()

        return router
