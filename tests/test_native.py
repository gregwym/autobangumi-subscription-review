import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from review_gate.configuration import PROVIDER
from review_gate.policy import Policy
from review_gate.versions import variants


def test_version_identity_does_not_match_other_group_or_missing_codec():
    item = {
        "name": "[Group] Show 01 1080p HEVC",
        "group": "Group",
        "resolution": "1080p",
        "episode": 1,
        "episode_key": "show/1",
        "first_seen": 1,
    }
    version = variants([item])[0]
    policy = Policy(include=version["include"], version_id=version["id"], mode="backlog")
    assert policy.reason(item) is None
    assert policy.reason({**item, "group": "Group Extra"})
    assert policy.reason({**item, "name": "[Group] Show 02 1080p"})
    assert variants([{**item, "resolution": None}])[0]["resolution"] is None


async def test_activate_preserves_config_and_reloads_outside_guard(plugin, tmp_path, monkeypatch):
    from module.conf import config, settings

    instance = settings.plugins.instances[0]
    instance.provider = "qbittorrent"
    instance.options["password"] = "test-secret"
    source = settings.model_dump(mode="json")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(source))
    monkeypatch.setattr(config, "CONFIG_PATH", path)

    async def reload():
        assert not plugin.bridge.lock.locked()
        saved = json.loads(path.read_text())
        assert saved["plugins"]["instances"][0]["provider"] == PROVIDER
        saved["plugins"]["instances"][0]["provider"] = "qbittorrent"
        assert saved == source

    context = SimpleNamespace(reload_settings=AsyncMock(side_effect=reload))
    result = await plugin.bridge.activate(context)
    assert result["enabled"]
    assert "test-secret" not in json.dumps(result)
    context.reload_settings.assert_awaited_once()
    assert len(list(tmp_path.glob("config.json.before-review-*"))) == 1


async def test_activation_rejects_unsupported_and_keeps_guard_on_reload_failure(
    plugin, tmp_path, monkeypatch
):
    from module.conf import config, settings

    path = tmp_path / "config.json"
    settings.plugins.instances[0].provider = "aria2"
    path.write_text(settings.model_dump_json())
    original = path.read_bytes()
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    context = SimpleNamespace(reload_settings=AsyncMock(side_effect=RuntimeError("reload failed")))
    with pytest.raises(ValueError):
        await plugin.bridge.activate(context)
    assert path.read_bytes() == original
    context.reload_settings.assert_not_called()
    settings.plugins.instances[0].provider = "qbittorrent"
    path.write_text(settings.model_dump_json())
    with pytest.raises(RuntimeError):
        await plugin.bridge.activate(context)
    assert json.loads(path.read_text())["plugins"]["instances"][0]["provider"] == PROVIDER


async def test_activation_endpoint_uses_real_ab_settings_reload(plugin, tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import Mock

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from module.conf import config, settings
    from module.core.context import AppContext

    settings.plugins.instances[0].provider = "qbittorrent"
    original_options = settings.plugins.instances[0].options.copy()
    path = tmp_path / "config.json"
    path.write_text(settings.model_dump_json())
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    # Use AB's actual reload choreography with background services replaced.
    context = object.__new__(AppContext)
    context._lifecycle_lock = asyncio.Lock()
    context.notifier = Mock()
    context.plugins = SimpleNamespace(apply_settings=AsyncMock())
    context.scheduler = SimpleNamespace(running=False)
    app = FastAPI()
    app.state.ctx = context
    app.include_router(plugin.api())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/activate")
    assert response.status_code == 200, response.text
    assert settings.plugins.instances[0].provider == PROVIDER
    assert settings.plugins.instances[0].options == original_options
    context.plugins.apply_settings.assert_awaited_once()
    assert (await plugin.bridge.health())["ready"]
