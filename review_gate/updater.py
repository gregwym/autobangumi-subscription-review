"""Unsigned local-plugin updates from this project's fixed public release channel."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from packaging.version import Version

from .audit import Audit, traced

REPO = "gregwym/autobangumi-subscription-review"
PLUGIN_ID = "subscription-review"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
MAX_ZIP = 8 * 1024 * 1024


def fetch(url, limit):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "AutoBangumi-SubscriptionReview",
            "Accept": "application/vnd.github+json" if url == API else "application/octet-stream",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("更新文件超出大小限制")
    return data


def release_info(raw, current):
    version = raw.get("tag_name", "").removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("更新版本格式不正确")
    parsed = Version(version)
    if raw.get("draft") or raw.get("prerelease") or parsed.is_prerelease:
        raise ValueError("此更新通道只接受稳定版")
    name = f"subscription-review-{version}.zip"
    base = f"https://github.com/{REPO}/releases/download/{raw['tag_name']}/"
    assets = {a["name"]: a["browser_download_url"] for a in raw.get("assets", [])}
    if assets.get(name) != base + name or assets.get(name + ".sha256") != base + name + ".sha256":
        raise ValueError("发布包或校验文件不属于固定更新来源")
    return {
        "version": version,
        "available": parsed > Version(current),
        "release_id": raw["id"],
        "url": f"https://github.com/{REPO}/releases/tag/{raw['tag_name']}",
        "archive": assets[name],
        "checksum": assets[name + ".sha256"],
        "notes": raw.get("body", "")[:6000],
    }


def unpack(data, checksum, stage, version):
    digest = hashlib.sha256(data).hexdigest()
    if len(checksum) != 64 or digest != checksum.lower():
        raise ValueError("更新包 SHA-256 校验失败")
    stage.mkdir(parents=True)
    archive = stage.parent / (stage.name + ".zip")
    archive.write_bytes(data)
    try:
        with zipfile.ZipFile(archive) as z:
            entries = z.infolist()
            if len(entries) > 200 or sum(i.file_size for i in entries) > 20 * 1024 * 1024:
                raise ValueError("更新包内容超出限制")
            seen = set()
            for item in entries:
                path = PurePosixPath(item.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in item.filename
                    or item.filename in seen
                    or stat.S_ISLNK(item.external_attr >> 16)
                ):
                    raise ValueError("更新包包含不安全路径")
                seen.add(item.filename)
                if item.is_dir():
                    continue
                allowed = (
                    item.filename in {"plugin.toml", "README.md", "LICENSE"}
                    or path.parts[0] == "review_gate"
                    and path.suffix == ".py"
                    or path.parts[0] == "web"
                    and path.suffix in {".js", ".css", ".map"}
                    or item.filename == "tools/configure.py"
                )
                if not allowed:
                    raise ValueError("更新包包含非插件文件")
                target = stage / item.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(item))
            manifest = tomllib.loads((stage / "plugin.toml").read_text())["plugin"]
            if manifest["id"] != PLUGIN_ID or manifest["version"] != version:
                raise ValueError("更新包身份或版本与发布不一致")
        from ab_sdk.manifest import check

        _, errors = check(stage)
        if errors:
            raise ValueError("更新包未通过插件清单校验")
        return digest
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    finally:
        archive.unlink(missing_ok=True)


class Updater:
    def __init__(self, root, data_dir, bridge, audit=None):
        self.audit = audit or Audit()
        self.root, self.data_dir, self.bridge = Path(root), Path(data_dir), bridge
        self.version = tomllib.loads((self.root / "plugin.toml").read_text())["plugin"]["version"]
        self.latest = None
        self.checked_at = None
        self.error = None
        self.lock = asyncio.Lock()

    def status(self):
        return {
            "current": self.version,
            "checked_at": self.checked_at,
            "error": self.error,
            **(
                {
                    k: v
                    for k, v in self.latest.items()
                    if k not in {"archive", "checksum", "release_id"}
                }
                if self.latest
                else {"available": False}
            ),
        }

    @traced("maintenance.update-check")
    async def check(self):
        async with self.lock:
            try:
                raw = json.loads(await asyncio.to_thread(fetch, API, 1024 * 1024))
                self.latest = release_info(raw, self.version)
                self.error = None
            except urllib.error.HTTPError as e:
                self.latest = None
                self.error = "尚无稳定版发布" if e.code == 404 else f"检查更新失败（HTTP {e.code}）"
            except Exception:
                self.latest = None
                self.error = "暂时无法检查更新，请稍后重试"
            self.checked_at = time.time()
            return self.status()

    @traced("update.install")
    async def install(self, version, context):
        async with self.lock:
            disk_version = tomllib.loads((self.root / "plugin.toml").read_text())["plugin"][
                "version"
            ]
            if disk_version != self.version:
                raise ValueError("插件已被更新，请刷新页面")
            # Re-read the trusted channel instead of accepting URLs from the browser.
            raw = json.loads(await asyncio.to_thread(fetch, API, 1024 * 1024))
            info = release_info(raw, self.version)
            if not info["available"] or info["version"] != version:
                raise ValueError("可用版本已变化，请重新检查更新")
            data = await asyncio.to_thread(fetch, info["archive"], MAX_ZIP)
            checksum_text = (await asyncio.to_thread(fetch, info["checksum"], 4096)).decode()
            parts = checksum_text.strip().split()
            if len(parts) != 2 or parts[1] != f"subscription-review-{version}.zip":
                raise ValueError("更新校验文件格式不正确")
            work = self.data_dir / "updates" / secrets.token_hex(8)
            stage, old = work / "new", work / "previous"
            await asyncio.to_thread(unpack, data, parts[0], stage, version)
            if os.stat(self.root.parent).st_dev != os.stat(work).st_dev:
                raise ValueError("插件与备份目录不在同一文件系统，无法原子更新")
            running = context.is_running
            if running:
                await context.stop()
            swapped = False
            backup = None
            try:
                async with self.bridge.replay_lock, self.bridge.lock:
                    backup = str(await asyncio.to_thread(self.bridge.maintenance().backup))
                    os.replace(self.root, old)
                    try:
                        os.replace(stage, self.root)
                    except BaseException:
                        os.replace(old, self.root)
                        raise
                    swapped = True
                await context.plugins.reload(PLUGIN_ID)
                state = next((s for s in context.plugins.statuses() if s.id == PLUGIN_ID), None)
                if not state or state.state != "active" or state.version != version:
                    raise ValueError("新版未能正常启动")
                result = {"installed": version, "backup": backup, "previous_code": str(old)}
                (work / "result.json").write_text(json.dumps(result))
                return result
            except BaseException as error:
                if swapped:
                    os.replace(self.root, work / "failed")
                    os.replace(old, self.root)
                    await context.plugins.reload(PLUGIN_ID)
                raise ValueError("更新未完成，已保留或恢复旧版；请检查插件状态") from error
            finally:
                if running:
                    await context.start_tasks()
