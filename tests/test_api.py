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
