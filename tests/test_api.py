import asyncio
import json

import pytest
from conftest import release
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.mark.parametrize(
    ("options", "mode", "expected"),
    [
        ({}, "backlog", 1),
        ({"mode": "future"}, "future", 0),
    ],
)
async def test_preview_then_approve_does_not_send_before_approval(
    plugin, rule, api, options, mode, expected
):
    await plugin.bridge.capture(rule, release())
    async with api:
        result = await api.post(
            f"/review/rules/{rule.id}/preview",
            json={"include": ["HEVC", "1080p"], **options},
        )
        assert result.status_code == 200, result.text
        assert result.json()["policy"]["mode"] == mode
        assert result.json()["will_download"] == expected
        plugin.test_send.assert_not_called()
        approved = await api.post(
            f"/review/rules/{rule.id}/approve",
            json={"preview_token": result.json()["preview_token"]},
        )
        assert approved.status_code == 200, approved.text
        versions = await api.get(f"/review/rules/{rule.id}/versions")
        assert versions.json()["rule"]["policy"]["mode"] == mode
    await plugin.bridge.replay()
    assert plugin.test_send.call_count == expected


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


async def test_read_versions_during_maintenance_and_find_newly_allowed_orphan(plugin, rule, api):
    from module.database import Database

    torrent = release(2, "AVC")
    async with Database() as db:
        stored = await db.bangumi.search_id(rule.id)
        stored.filter = "AVC"
        db.add(stored)
        db.add(torrent)
        await db.commit()
    async with api:
        # A read while the maintenance lock is held must finish without writing.
        async with plugin.bridge.lock, asyncio.timeout(2):
            result = await api.get(f"/review/rules/{rule.id}/versions")
        assert result.status_code == 200, result.text
        assert result.json()["rule"]["state"] == "pending"
        assert not result.json()["versions"]
        assert not await plugin.store.rules(rule.id)
        async with Database() as db:
            assert (await db.torrent.search(torrent.id)).bangumi_id is None
            stored = await db.bangumi.search_id(rule.id)
            stored.filter = ""
            db.add(stored)
            await db.commit()

        await plugin.bridge.replay()
        result = await api.get(f"/review/rules/{rule.id}/versions")
        assert result.status_code == 200
        assert result.json()["versions"][0]["codec"] == "AVC"
    plugin.test_send.assert_not_called()


async def test_offset_change_blocks_old_preview_until_background_sync(plugin, rule, api):
    from module.database import Database

    await plugin.bridge.capture(rule, release(12))
    async with api:
        preview = await api.post(f"/review/rules/{rule.id}/preview", json={"unfiltered": True})
        assert preview.status_code == 200, preview.text
        async with Database() as db:
            stored = await db.bangumi.search_id(rule.id)
            stored.episode_offset = -10
            db.add(stored)
            await db.commit()
        versions = (await api.get(f"/review/rules/{rule.id}/versions")).json()
        assert versions["needs_sync"] and not versions["versions"]
        confirmed = await api.post(
            f"/review/rules/{rule.id}/approve",
            json={"preview_token": preview.json()["preview_token"]},
        )
        assert confirmed.status_code == 409

        await plugin.bridge.replay()
        versions = (await api.get(f"/review/rules/{rule.id}/versions")).json()
        assert not versions["needs_sync"]
        assert versions["rule"]["episode_offset"] == -10
        [candidate] = await plugin.store.candidates(rule.id)
        assert json.loads(candidate["episode_key"])[3] == 2
    plugin.test_send.assert_not_called()


async def test_same_show_download_after_preview_requires_new_confirmation(plugin, rule, api):
    from module.database import Database
    from module.models import Bangumi

    await plugin.bridge.capture(rule, release())
    async with api:
        path = f"/review/rules/{rule.id}"
        options = {"unfiltered": True}
        preview = (await api.post(f"{path}/preview", json=options)).json()
        assert preview["will_download"] == 1
        async with Database() as db:
            sibling = Bangumi(
                official_title=rule.official_title,
                title_raw=rule.title_raw,
                season=rule.season,
                group_name="Other",
            )
            db.add(sibling)
            await db.session.flush()
            downloaded = release(url="magnet:?external")
            downloaded.bangumi_id = sibling.id
            downloaded.downloaded = True
            db.add(downloaded)
            await db.commit()
        confirmed = await api.post(
            f"{path}/approve", json={"preview_token": preview["preview_token"]}
        )
        assert confirmed.status_code == 409
        preview = (await api.post(f"{path}/preview", json=options)).json()
        assert preview["will_download"] == 0
        assert preview["items"][0]["reason"] == "同版本已有下载记录"
        confirmed = await api.post(
            f"{path}/approve", json={"preview_token": preview["preview_token"]}
        )
        assert confirmed.status_code == 200, confirmed.text
    await plugin.bridge.replay()
    plugin.test_send.assert_not_called()


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


async def test_multi_version_or_semantics_keeps_distinct_versions(plugin, rule, api):
    for ep, codec in [(1, "HEVC"), (1, "AVC"), (2, "AVC"), (3, "x265")]:
        await plugin.bridge.capture(rule, release(ep, codec))
    async with api:
        versions = (await api.get(f"/review/rules/{rule.id}/versions")).json()["versions"]
        ids = [v["id"] for v in versions if v["codec"] in ("HEVC", "AVC")]
        preview = await api.post(
            f"/review/rules/{rule.id}/preview", json={"version_ids": ids, "mode": "backlog"}
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["download_counts"] == {"single": 3, "batch": 0, "feature": 0}
        response = await api.post(
            f"/review/rules/{rule.id}/approve",
            json={"preview_token": preview.json()["preview_token"]},
        )
        assert response.status_code == 200
        invalid = await api.post(
            f"/review/rules/{rule.id}/preview", json={"version_ids": ids, "unfiltered": True}
        )
        assert invalid.status_code == 422
        unknown = await api.post(
            f"/review/rules/{rule.id}/preview", json={"version_ids": ["unknown"]}
        )
        assert unknown.status_code == 409
    await plugin.bridge.replay()
    assert plugin.test_send.call_count == 3
    assert "x265" not in str(plugin.test_send.call_args_list)
