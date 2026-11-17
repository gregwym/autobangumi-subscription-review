from __future__ import annotations

import hashlib
import json
import secrets
import time

from ab_sdk import Plugin, Verdict, hook, points, provider
from ab_sdk.tasks import ScheduledTask
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .bridge40 import PROVIDER, Bridge40
from .policy import Policy
from .store import Store
from .versions import variants


class ReviewRequest(BaseModel):
    include: list[str] = Field(default_factory=list, max_length=32)
    version_id: str | None = Field(default=None, max_length=40)
    exclude: list[str] = Field(default_factory=list, max_length=32)
    mode: str = "future"
    from_episode: float = Field(default=1, ge=0, le=100000)


class ApprovalRequest(BaseModel):
    preview_token: str = Field(min_length=20, max_length=100)


def snapshot(revision, candidates):
    rows = [(c["id"], c["name"], c["status"]) for c in candidates]
    return hashlib.sha256(json.dumps([revision, rows], sort_keys=True).encode()).hexdigest()


class SubscriptionReview(Plugin):
    async def setup(self):
        self.previews = {}
        self.store = Store(self.ctx.data_dir / "review.sqlite3")
        await self.store.init()
        self.bridge = Bridge40(self.store, self.ctx.log)
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

    @provider(points.API_ROUTER, id="api")
    def api(self):
        router = APIRouter()

        async def find_rule(rid):
            for r in await self.store.rules():
                if r["id"] == rid:
                    return r
            raise HTTPException(404, "未找到番剧规则")

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

        @router.post("/sync")
        async def sync():
            await self.bridge.sync_history()
            return {"synced": True}

        @router.post("/replay")
        async def replay():
            return await self.bridge.replay()

        return router
