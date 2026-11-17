import pytest
from conftest import release
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def api(plugin):
    app = FastAPI()
    app.include_router(plugin.api(), prefix="/review")
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_preview_then_approve_does_not_send_before_approval(plugin, rule, api):
    await plugin.bridge.capture(rule, release())
    async with api:
        result = await api.post(
            f"/review/rules/{rule.id}/preview",
            json={"include": ["HEVC", "1080p"], "mode": "backlog"},
        )
        assert result.status_code == 200, result.text
        assert result.json()["will_download"] == 1
        plugin.test_send.assert_not_called()
        approved = await api.post(
            f"/review/rules/{rule.id}/approve",
            json={"preview_token": result.json()["preview_token"]},
        )
        assert approved.status_code == 200, approved.text
    await plugin.bridge.replay()
    assert plugin.test_send.call_count == 1


async def test_changed_candidates_invalidate_preview(plugin, rule, api):
    await plugin.bridge.capture(rule, release())
    async with api:
        result = await api.post(
            f"/review/rules/{rule.id}/preview", json={"include": ["HEVC"], "mode": "backlog"}
        )
        await plugin.bridge.capture(rule, release(2))
        response = await api.post(
            f"/review/rules/{rule.id}/approve",
            json={"preview_token": result.json()["preview_token"]},
        )
        assert response.status_code == 409


async def test_empty_include_rejected_and_urls_redacted(plugin, rule, api):
    await plugin.bridge.capture(rule, release(url="https://example.test/file?token=secret"))
    async with api:
        response = await api.post(f"/review/rules/{rule.id}/preview", json={"include": []})
        assert response.status_code == 422
        candidates = await api.get(f"/review/rules/{rule.id}/candidates")
        assert "secret" not in candidates.text
        assert "url" not in candidates.json()[0]


async def test_native_version_selection_groups_episodes_and_filters_codec(plugin, rule, api):
    for ep, codec in [(1, "HEVC"), (2, "HEVC"), (1, "AVC")]:
        await plugin.bridge.capture(rule, release(ep, variant=codec))
    async with api:
        response = await api.get(f"/review/rules/{rule.id}/versions")
        assert response.status_code == 200
        versions = response.json()["versions"]
        assert len(versions) == 2
        hevc = next(v for v in versions if v["codec"] == "HEVC")
        assert hevc["count"] == 2
        assert "magnet:" not in response.text
        response = await api.post(
            f"/review/rules/{rule.id}/preview", json={"version_id": hevc["id"], "mode": "backlog"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["will_download"] == 2
        plugin.test_send.assert_not_called()
        response = await api.post(
            f"/review/rules/{rule.id}/approve",
            json={"preview_token": response.json()["preview_token"]},
        )
        assert response.status_code == 200
    await plugin.bridge.replay()
    assert plugin.test_send.call_count == 2
    assert all("HEVC" in str(call) for call in plugin.test_send.call_args_list)


async def test_unknown_version_and_ambiguous_conditions_refused(plugin, rule, api):
    await plugin.bridge.capture(rule, release())
    async with api:
        response = await api.post(
            f"/review/rules/{rule.id}/preview", json={"version_id": "unknown"}
        )
        assert response.status_code == 409
        data = (await api.get(f"/review/rules/{rule.id}/versions")).json()
        response = await api.post(
            f"/review/rules/{rule.id}/preview",
            json={"version_id": data["versions"][0]["id"], "include": ["HEVC"]},
        )
        assert response.status_code == 422
    plugin.test_send.assert_not_called()


async def test_unfiltered_api_keeps_download_gate(plugin, rule, api):
    await plugin.bridge.capture(rule, release())
    async with api:
        preview = await api.post(
            f"/review/rules/{rule.id}/preview", json={"unfiltered": True, "mode": "backlog"}
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["will_download"] == 1
        plugin.test_send.assert_not_called()
        ambiguous = await api.post(
            f"/review/rules/{rule.id}/preview", json={"unfiltered": True, "version_id": "anything"}
        )
        assert ambiguous.status_code == 422


async def test_history_api_preview_clear_lifecycle_and_secret_redaction(plugin, rule):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from test_maintenance import add_history

    await add_history(plugin, rule)
    context = SimpleNamespace(is_running=True, stop=AsyncMock(), start_tasks=AsyncMock())
    app = FastAPI()
    app.state.ctx = context
    app.include_router(plugin.api())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post(f"/rules/{rule.id}/history/preview", json={"episodes": [1]})
        assert result.status_code == 200, result.text
        assert "magnet:" not in result.text
        assert "snapshot" not in result.text
        clear = await client.post(
            f"/rules/{rule.id}/history/clear",
            json={"preview_token": result.json()["preview_token"]},
        )
        assert clear.status_code == 200, clear.text
        assert clear.json()["cleared_episodes"] == [1]
        reused = await client.post(
            f"/rules/{rule.id}/history/clear",
            json={"preview_token": result.json()["preview_token"]},
        )
        assert reused.status_code == 409
    context.stop.assert_awaited_once()
    context.start_tasks.assert_awaited_once()
    plugin.test_send.assert_not_called()
