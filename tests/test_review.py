import asyncio
import time

import pytest
from conftest import approve, release


async def test_pending_rule_does_not_download_even_without_filter_hook(plugin, rule):
    from ab_sdk.downloader import AddResult
    from module.downloader import DownloadClient

    async with DownloadClient() as client:
        result = await client.add_torrent(release(), rule)
    assert result is AddResult.FAILED
    plugin.test_send.assert_not_called()
    assert len(await plugin.store.candidates(rule.id)) == 1


async def test_approved_only_matching_version_downloads(plugin, rule):
    from ab_sdk.downloader import AddResult
    from module.downloader import DownloadClient

    await approve(plugin, rule)
    async with DownloadClient() as client:
        assert await client.add_torrent(release(1, "AVC"), rule) is AddResult.FAILED
        assert await client.add_torrent(release(1), rule) is AddResult.ADDED
        assert await client.add_torrent(release(2, "AVC"), rule) is AddResult.FAILED
    assert plugin.test_send.call_count == 1
    assert plugin.test_send.call_args.args[4] == f"ab:{rule.id}"


async def test_concurrent_same_episode_only_one_version_sent(plugin, rule):
    from module.downloader import DownloadClient

    await approve(plugin, rule)
    async with DownloadClient() as client:
        await asyncio.gather(
            client.add_torrent(release(url="magnet:?a"), rule),
            client.add_torrent(release(url="magnet:?b"), rule),
        )
    assert plugin.test_send.call_count == 1


async def test_same_url_idempotent(plugin, rule):
    from ab_sdk.downloader import AddResult
    from module.downloader import DownloadClient

    await approve(plugin, rule)
    async with DownloadClient() as client:
        await client.add_torrent(release(), rule)
        assert await client.add_torrent(release(), rule) is AddResult.DUPLICATE
    assert plugin.test_send.call_count == 1


async def test_guard_rejects_direct_backend_call_and_after_teardown(plugin):
    from ab_sdk.downloader import AddResult, DownloaderConnection

    qb = plugin.downloader()(DownloaderConnection("host", "user", "password", False, "default"))
    assert (
        await qb.add_torrents("magnet:?a", None, "/downloads", "Bangumi", "ab:1")
        is AddResult.FAILED
    )
    await plugin.teardown()
    assert (
        await qb.add_torrents("magnet:?a", None, "/downloads", "Bangumi", "ab:1")
        is AddResult.FAILED
    )
    plugin.test_send.assert_not_called()


async def test_failure_retry_does_not_consume_episode(plugin, rule):
    from ab_sdk.downloader import AddResult
    from module.downloader import DownloadClient

    await approve(plugin, rule)
    plugin.test_send.side_effect = [AddResult.FAILED, AddResult.ADDED]
    async with DownloadClient() as client:
        assert await client.add_torrent(release(), rule) is AddResult.FAILED
        assert await client.add_torrent(release(), rule) is AddResult.ADDED
    assert plugin.test_send.call_count == 2


async def test_future_only_and_start_episode(plugin, rule):
    from module.downloader import DownloadClient

    from review_gate.policy import Policy

    old = release(5)
    await plugin.bridge.capture(rule, old)
    rows = await plugin.store.rules()
    await plugin.store.set_policy(
        rule.id,
        Policy(include=["HEVC"], mode="future", cutoff=time.time(), from_episode=6),
        rows[0]["revision"],
    )
    async with DownloadClient() as client:
        await client.add_torrent(old, rule)
        await client.add_torrent(release(4), rule)
        await client.add_torrent(release(6), rule)
    assert plugin.test_send.call_count == 1


async def test_renamed_or_reused_rule_requires_review(plugin, rule):
    from module.downloader import DownloadClient

    await approve(plugin, rule)
    rule.official_title = "另一部番"
    async with DownloadClient() as client:
        await client.add_torrent(release(), rule)
    plugin.test_send.assert_not_called()
    assert (await plugin.store.rules())[0]["state"] == "pending"


async def test_unparseable_collection_held(plugin, rule):
    from module.downloader import DownloadClient
    from module.models import Torrent

    await approve(plugin, rule)
    torrent = Torrent(
        name="[Group] Example Show [01-12][1080p HEVC][内封]", url="magnet:?collection"
    )
    async with DownloadClient() as client:
        await client.add_torrent(torrent, rule)
    plugin.test_send.assert_not_called()


async def test_history_prevents_different_url_or_codec_redownload(plugin, rule):
    from module.database import Database
    from module.downloader import DownloadClient

    old = release(1, "AVC", "magnet:?old")
    old.downloaded, old.bangumi_id = True, rule.id
    async with Database() as db:
        db.add(old)
        await db.commit()
    await plugin.bridge.sync_history()
    await approve(plugin, rule)
    async with DownloadClient() as client:
        await client.add_torrent(release(1), rule)
        await client.add_torrent(release(2), rule)
    assert plugin.test_send.call_count == 1


async def test_orphan_replay_repairs_existing_host_row(plugin, rule):
    from module.database import Database
    from module.models import Torrent
    from sqlmodel import select

    torrent = release()
    await plugin.bridge.capture(rule, torrent)
    async with Database() as db:
        db.add(torrent)  # Simulate AB track_orphans=True after rejection.
        await db.commit()
    await approve(plugin, rule)
    result = await plugin.bridge.replay()
    assert result["sent"] == 1
    async with Database() as db:
        rows = (
            (await db.session.execute(select(Torrent).where(Torrent.url == torrent.url)))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].downloaded and rows[0].bangumi_id == rule.id
        assert rows[0].downloader_id == "default"
    await plugin.bridge.replay()
    assert plugin.test_send.call_count == 1


async def test_network_success_before_host_commit_repaired_without_resend(plugin, rule):
    from module.database import Database
    from module.downloader import DownloadClient
    from module.models import Torrent
    from sqlmodel import select

    await approve(plugin, rule)
    async with DownloadClient() as client:
        await client.add_torrent(release(), rule)
    await plugin.bridge.replay()
    async with Database() as db:
        rows = (await db.session.execute(select(Torrent))).scalars().all()
        assert len(rows) == 1 and rows[0].downloaded
    assert plugin.test_send.call_count == 1


async def test_store_persists_pending_across_restart(plugin, rule):
    from review_gate.store import Store

    await plugin.bridge.capture(rule, release())
    other = Store(plugin.store.path)
    await other.init()
    assert len(await other.candidates()) == 1
    assert (await other.rules())[0]["state"] == "pending"


async def test_corrupt_store_is_fail_closed(plugin, rule, monkeypatch):
    from module.downloader import DownloadClient

    async def failed(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(plugin.store, "capture", failed)
    async with DownloadClient() as client:
        with pytest.raises(OSError):
            await client.add_torrent(release(), rule)
    plugin.test_send.assert_not_called()


async def test_version_mismatch_does_not_patch_host(plugin, monkeypatch, tmp_path):
    from module import conf
    from module.downloader import DownloadClient

    from review_gate.bridge40 import Bridge40
    from review_gate.store import Store

    await plugin.teardown()
    original = DownloadClient.add_torrent
    monkeypatch.setattr(conf, "VERSION", "4.0.1")
    other = Bridge40(Store(tmp_path / "other.db"), plugin.ctx.log)
    with pytest.raises(RuntimeError, match="4.0.0"):
        await other.setup()
    assert DownloadClient.add_torrent is original


async def test_hold_keeps_downloaded_tasks_untouched(plugin, rule):
    from module.downloader import DownloadClient

    await approve(plugin, rule)
    async with DownloadClient() as client:
        await client.add_torrent(release(1), rule)
        await plugin.store.hold(rule.id)
        await client.add_torrent(release(2), rule)
    assert plugin.test_send.call_count == 1


async def test_unguarded_instance_blocks_approval_health(plugin):
    from module.conf import settings

    settings.plugins.instances[0].provider = "qbittorrent"
    assert not (await plugin.bridge.health())["ready"]


async def test_uncertain_network_outcome_reserves_episode_across_restart(plugin, rule):
    from module.downloader import DownloadClient

    from review_gate.store import Store

    await approve(plugin, rule)
    plugin.test_send.side_effect = TimeoutError("outcome unknown")
    async with DownloadClient() as client:
        # AB catches network errors as FAILED; the durable reservation survives.
        await client.add_torrent(release(url="magnet:?original"), rule)
    plugin.bridge.store = Store(plugin.store.path)
    await plugin.bridge.store.init()
    async with DownloadClient() as client:
        await client.add_torrent(release(url="magnet:?other-version"), rule)
    assert plugin.test_send.call_count == 1


async def test_replayed_candidate_keeps_approval_after_plugin_restart(plugin, rule):
    from ab_sdk import points
    from ab_sdk.testing import create_plugin
    from module.plugin import host
    from module.plugin.registry import ProviderEntry

    from review_gate.bridge40 import PROVIDER
    from review_gate.plugin import SubscriptionReview

    await plugin.bridge.capture(rule, release())
    await approve(plugin, rule)
    await plugin.teardown()
    host.get_registry().remove_plugin("subscription-review")
    replacement, _ = create_plugin(
        SubscriptionReview, plugin_id="subscription-review", data_dir=plugin.ctx.data_dir
    )
    await replacement.setup()
    host.get_registry().add_provider(
        points.DOWNLOADER, ProviderEntry("subscription-review", PROVIDER, replacement.downloader)
    )
    try:
        result = await replacement.bridge.replay()
        assert result["sent"] == 1
        assert plugin.test_send.call_count == 1
    finally:
        await replacement.teardown()


async def test_history_batch_is_atomic_and_deduplicates_existing_downloads(plugin, rule):
    from review_gate.policy import Policy

    r = plugin.bridge.rule_data(rule)
    entries = []
    for ep in range(1, 123):
        t = release(ep)
        entries.append(
            (r, plugin.bridge.torrent_data(t), plugin.bridge.parsed_data(rule, t.name), ep < 100)
        )
    await plugin.store.import_history([r], entries)
    assert len(await plugin.store.candidates(rule.id)) == 122
    await plugin.store.import_history([r], entries)
    assert len(await plugin.store.candidates(rule.id)) == 122
    candidate = next(c for c in await plugin.store.candidates(rule.id) if c["episode"] == 1)
    assert candidate["status"] == "delivered"
    assert (
        await plugin.store.decision(candidate, override=Policy(include=["HEVC"], mode="backlog"))
        == "已投递"
    )
    # A failed batch must not leave a partially imported new rule or candidate.
    other = {**r, "id": rule.id + 1000, "official_title": "Atomic failure"}
    invalid = (other, {"url": "invalid"}, {}, False)
    with pytest.raises(TypeError):
        await plugin.store.import_history(
            [other], [(*entries[0][:3], True), (other, {"url": object()}, {}, False), invalid]
        )
    assert all(x["id"] != other["id"] for x in await plugin.store.rules())
