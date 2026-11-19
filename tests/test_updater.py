import hashlib
import inspect
import io
import json
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from review_gate.updater import API, PLUGIN_ID, REPO, release_info, unpack
from tools.build import build

ROOT = Path(__file__).resolve().parents[1]


def release_record(version="0.4.0"):
    name = f"subscription-review-{version}.zip"
    base = f"https://github.com/{REPO}/releases/download/v{version}/"
    return {
        "id": 1,
        "tag_name": f"v{version}",
        "assets": [{"name": n, "browser_download_url": base + n} for n in (name, name + ".sha256")],
    }


def test_release_channel_rejects_foreign_assets_and_prereleases():
    raw = release_record()
    assert release_info(raw, "0.3.1")["available"]
    assert not release_info(raw, "0.4.0")["available"]
    raw["assets"][0]["browser_download_url"] = "https://example.test/file.zip"
    with pytest.raises(ValueError):
        release_info(raw, "0.3.1")
    raw = {**release_record(), "prerelease": True}
    with pytest.raises(ValueError):
        release_info(raw, "0.3.1")


@pytest.mark.parametrize(
    "name", ["../escape.py", "/escape.py", "web/../../escape.py", "review_gate\\escape.py"]
)
def test_bad_archive_cannot_escape_staging(tmp_path, name):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        z.writestr(name, "bad")
    data = stream.getvalue()
    with pytest.raises(ValueError):
        unpack(data, hashlib.sha256(data).hexdigest(), tmp_path / "stage", "0.4.0")
    assert not (tmp_path / "stage").exists()
    assert not (tmp_path / "escape.py").exists()


def test_wrong_checksum_does_not_extract(tmp_path):
    with pytest.raises(ValueError, match="SHA-256"):
        unpack(b"invalid", "0" * 64, tmp_path / "stage", "0.4.0")
    assert not (tmp_path / "stage").exists()


@pytest.mark.parametrize("broken", [False, True])
async def test_update_real_host_loader_and_rollback(tmp_path, monkeypatch, broken):
    from module.conf import settings
    from module.plugin.host import get_registry
    from module.plugin.loader import discover
    from module.plugin.manager import PluginManager

    local = tmp_path / "local"
    root = local / PLUGIN_ID
    shutil.copytree(
        ROOT,
        root,
        ignore=shutil.ignore_patterns(
            ".git", ".pytest_cache", "__pycache__", "dist", ".ruff_cache"
        ),
    )
    manifest = (root / "plugin.toml").read_text()
    import re

    manifest = re.sub(r'version = "[^"]+"', 'version = "0.3.2"', manifest, count=1)
    (root / "plugin.toml").write_text(manifest)
    settings.plugins.allow_unsigned = True
    settings.plugins.enabled[PLUGIN_ID] = True
    manager = PluginManager(
        settings,
        registry=get_registry(),
        data_root=tmp_path / "data",
        discover_fn=lambda: discover(
            builtin_root=tmp_path / "none",
            catalog_root=tmp_path / "none",
            local_root=local,
            entry_point_group="test.no.plugins",
        ),
    )
    await manager.start()
    try:
        assert manager.statuses()[0].state == "active"
        updater = manager._active[PLUGIN_ID].instance.updater
        new = tmp_path / "newcode"
        shutil.copytree(root, new)
        (new / "plugin.toml").write_text(manifest.replace('"0.3.2"', '"0.4.0"'))
        if broken:
            entry = new / "review_gate/plugin.py"
            entry.write_text(
                entry.read_text().replace(
                    "    async def setup(self):",
                    "    async def setup(self):\n        raise RuntimeError('test broken update')",
                )
            )
        archive = build(new)
        info = release_info(release_record(), "0.3.2")
        payloads = {
            API: json.dumps(release_record()).encode(),
            info["archive"]: archive.read_bytes(),
            info["checksum"]: archive.with_suffix(".zip.sha256").read_bytes(),
        }
        # Patch the isolated module loaded by AB, not the source checkout.
        monkeypatch.setitem(
            inspect.unwrap(updater.check).__globals__, "fetch", lambda url, limit: payloads[url]
        )
        context = SimpleNamespace(
            is_running=True, stop=AsyncMock(), start_tasks=AsyncMock(), plugins=manager
        )
        assert (await updater.check())["available"]
        if broken:
            with pytest.raises(ValueError, match="恢复旧版"):
                await updater.install("0.4.0", context)
            assert manager.statuses()[0].version == "0.3.2"
        else:
            result = await updater.install("0.4.0", context)
            assert Path(result["backup"], "ab.sqlite3").exists()
            assert Path(result["previous_code"], "plugin.toml").exists()
            assert manager.statuses()[0].version == "0.4.0"
            with pytest.raises(ValueError, match="已被更新"):
                await updater.install("0.4.0", context)
        assert manager.statuses()[0].state == "active"
        context.stop.assert_awaited_once()
        context.start_tasks.assert_awaited_once()
    finally:
        await manager.stop()
