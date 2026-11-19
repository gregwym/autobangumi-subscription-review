import json
import logging

from conftest import approve, release

from review_gate.audit import Audit


def events(caplog):
    return [
        json.loads(row.message.removeprefix("audit "))
        for row in caplog.records
        if row.message.startswith("audit ")
    ]


def test_audit_redacts_signed_urls_and_suppresses_unchanged_heartbeats(caplog):
    caplog.set_level(logging.DEBUG)
    audit = Audit()
    audit.emit(
        "download.retry",
        detail="https://user:password@example.test/rss?token=secret magnet:?xt=private",
    )
    assert (
        "password" not in caplog.text
        and "secret" not in caplog.text
        and "private" not in caplog.text
    )
    caplog.clear()
    audit.periodic("maintenance.queue", sent=0, held=1)
    audit.periodic("maintenance.queue", sent=0, held=1)
    assert [r.levelno for r in caplog.records] == [logging.INFO, logging.DEBUG]


async def test_native_dispatch_logs_success_without_claiming_download_completion(
    plugin, rule, caplog
):
    from module.downloader import DownloadClient

    await approve(plugin, rule)
    caplog.set_level(logging.INFO)
    torrent = release(
        2, url="magnet:?xt=urn:btih:1234567890123456789012345678901234567890&token=private"
    )
    async with DownloadClient() as client:
        await client.add_torrent(torrent, rule)
        await client.add_torrent(torrent, rule)
    rows = [r for r in events(caplog) if r["event"] == "download.submitted"]
    assert len(rows) == 1 and rows[0]["result"] == "added"
    assert rows[0]["rule_id"] == rule.id
    assert "尚非下载完成" in rows[0]["detail"]
    assert "private" not in caplog.text


async def test_queue_tick_reports_maintenance_and_retries_redacted_errors(plugin, rule, caplog):
    await approve(plugin, rule)
    await plugin.bridge.capture(rule, release(2))
    plugin.test_send.side_effect = OSError("https://example.test/?token=private failed")
    caplog.set_level(logging.DEBUG)
    result = await plugin.bridge.replay()
    assert result["sent"] == 0
    rows = events(caplog)
    assert any(r["event"] == "download.retry" and r["run"] for r in rows)
    assert any(r["event"] == "maintenance.queue" and r["reads"] and r["writes"] for r in rows)
    assert "private" not in "\n".join(
        r.message for r in caplog.records if r.message.startswith("audit ")
    )


async def test_api_logs_mutation_scope_without_preview_receipt(plugin, rule, caplog):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    app = FastAPI()
    app.include_router(plugin.api())
    caplog.set_level(logging.INFO)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/rules/{rule.id}/approve",
            json={"preview_token": "private-receipt-must-not-be-logged"},
        )
    assert response.status_code == 409
    assert "private-receipt" not in caplog.text
    assert any(
        r["event"] == "api.start" and r["params"]["rid"] == str(rule.id) for r in events(caplog)
    )
