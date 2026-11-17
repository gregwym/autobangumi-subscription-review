from unittest.mock import AsyncMock

import pytest
from conftest import approve, release


def install_hook(plugin, callback=None):
    from ab_sdk import points
    from module.plugin import host
    from module.plugin.registry import HookEntry
    from module.plugin.runner import CircuitBreaker, HookRunner

    registry = host.get_registry()
    registry.add_hook(
        points.TORRENT_FILTER, HookEntry("subscription-review", callback or plugin.review, 1, 30)
    )
    host.set_runner(HookRunner(registry, CircuitBreaker()))


async def test_aggregate_discovery_orphans_approval_then_new_episode(plugin, monkeypatch):
    from module.database import Database
    from module.downloader import DownloaderPool
    from module.models import RSSItem, Torrent
    from module.rss import RSSAnalyser, RSSEngine

    install_hook(plugin)
    async with Database() as db:
        rss = RSSItem(
            name="Test aggregate", url="https://example.test/rss", aggregate=True, parser="none"
        )
        db.add(rss)
        await db.commit()
    episode = 1

    async def pull(item):
        rows = [
            release(episode),
            release(episode, "AVC"),
            Torrent(
                name=f"[Other] Another Show - {episode:02} [1080p HEVC][简繁内封]",
                url=f"magnet:?another-{episode}",
            ),
        ]
        for r in rows:
            r.rss_id = item.id
        return rows

    monkeypatch.setattr(RSSEngine, "_get_torrents", staticmethod(pull))

    async def refresh():
        async with Database() as db, DownloaderPool() as pool:
            return await RSSEngine(db).refresh_rss(pool, analyser=RSSAnalyser())

    await refresh()
    plugin.test_send.assert_not_called()
    async with Database() as db:
        rules = await db.bangumi.search_all()
        assert len(rules) == 2
        assert len(await db.torrent.search_orphans()) == 3
    chosen = next(r for r in rules if r.title_raw == "Example Show")
    await approve(plugin, chosen)
    await plugin.bridge.replay()
    assert plugin.test_send.call_count == 1
    # Regular RSS polling (not replay) handles subsequent approved episodes.
    episode = 2
    await refresh()
    assert plugin.test_send.call_count == 2
    async with Database() as db:
        downloaded = [t for t in await db.torrent.search_all() if t.downloaded]
        assert len(downloaded) == 2
        assert all("HEVC" in t.name and "Example Show" in t.name for t in downloaded)


async def test_real_fail_open_runner_cannot_bypass_guard(plugin, rule, monkeypatch):
    from module.database import Database
    from module.downloader import DownloaderPool
    from module.models import RSSItem
    from module.rss import RSSEngine

    async def broken(*args):
        raise RuntimeError("filter crashed")

    install_hook(plugin, broken)
    async with Database() as db:
        rss = RSSItem(name="RSS", url="https://example.test/rss")
        db.add(rss)
        await db.commit()
    monkeypatch.setattr(
        RSSEngine, "_get_torrents", staticmethod(AsyncMock(return_value=[release()]))
    )
    async with Database() as db, DownloaderPool() as pool:
        await RSSEngine(db).refresh_rss(pool)
    plugin.test_send.assert_not_called()
    assert len(await plugin.store.candidates()) == 1


async def test_uninstalled_provider_does_not_fallback_to_qb(plugin):
    from module.downloader import DownloadClient
    from module.plugin import host

    await plugin.teardown()
    host.get_registry().remove_plugin("subscription-review")
    with pytest.raises(Exception, match="Unsupported downloader"):
        DownloadClient()
    plugin.test_send.assert_not_called()


async def test_real_plugin_loader_imports_package(tmp_path):
    import shutil
    from pathlib import Path

    from module.plugin.loader import _directory_candidate

    root = Path(__file__).resolve().parents[1]
    dest = tmp_path / "subscription-review"
    shutil.copytree(root / "review_gate", dest / "review_gate")
    shutil.copytree(root / "web", dest / "web")
    shutil.copy(root / "plugin.toml", dest / "plugin.toml")
    candidate = _directory_candidate(dest, "local")
    cls = candidate.load()
    assert cls.__name__ == "SubscriptionReview"
    candidate.unload()
