import asyncio
import json
import sqlite3

import pytest
from conftest import release

from review_gate.policy import Policy


async def add_history(plugin, rule, ep=1):
    from module.database import Database

    t = release(ep)
    t.bangumi_id = rule.id
    t.downloaded = True
    async with Database() as db:
        db.add(t)
        await db.commit()
    await plugin.bridge.sync_history()
    return t


async def test_clear_cross_group_history_backup_and_restart(plugin, rule):
    from module.database import Database
    from module.models import Bangumi

    old = await add_history(plugin, rule)
    async with Database() as db:
        sibling = Bangumi(
            official_title=rule.official_title,
            title_raw=rule.title_raw,
            season=1,
            group_name="Other",
            filter="",
        )
        db.add(sibling)
        await db.commit()
    await plugin.bridge.capture(sibling, release(1, "AVC"))
    await plugin.bridge.capture(rule, release(2))
    await plugin.store.set_policy(rule.id, Policy(include=["HEVC"], mode="backlog"), 0)
    maintenance = plugin.bridge.maintenance()
    preview = await asyncio.to_thread(maintenance.preview, sibling.id, [1])
    assert preview["summary"]["episodes"][0]["host_records"] == 1
    result = await asyncio.to_thread(maintenance.clear, preview)
    assert result["host_records"] == 1
    assert result["held_rules"] == [rule.id, sibling.id]
    async with Database() as db:
        assert await db.torrent.search(old.id) is None
        assert await db.bangumi.search_id(rule.id)
    with sqlite3.connect(result["backup"] + "/ab.sqlite3") as db:
        assert db.execute("select downloaded from torrent where id=?", (old.id,)).fetchone()[0] == 1
    with sqlite3.connect(result["backup"] + "/review.sqlite3") as db:
        assert db.execute("select count(*) from delivered").fetchone()[0] == 1
    await plugin.bridge.sync_history()
    assert not maintenance.history(rule.id)["episodes"]
    assert all(r["state"] == "pending" for r in await plugin.store.rules())
    assert len(await plugin.store.candidates()) == 3
    await plugin.bridge.replay()
    plugin.test_send.assert_not_called()


async def test_history_clear_refuses_stale_and_keeps_other_episode(plugin, rule):
    await add_history(plugin, rule, 1)
    await add_history(plugin, rule, 2)
    m = plugin.bridge.maintenance()
    preview = m.preview(rule.id, [1])
    await plugin.bridge.capture(rule, release(3))
    with pytest.raises(ValueError, match="记录已变化"):
        m.clear(preview)
    result = await asyncio.to_thread(m.clear, m.preview(rule.id, [1]))
    assert result["cleared_episodes"] == [1]
    assert [r["episode"] for r in m.history(rule.id)["episodes"]] == [2]


async def test_deleted_rule_plugin_history_can_be_cleared(plugin, rule):
    from module.database import Database

    await add_history(plugin, rule)
    async with Database() as db:
        from sqlalchemy import text

        await db.session.execute(text("DELETE FROM torrent"))
        await db.session.execute(text("DELETE FROM bangumi"))
        await db.commit()
    m = plugin.bridge.maintenance()
    assert m.rules()[0]["missing"]
    assert m.history(rule.id)["episodes"][0]["episode"] == 1
    await asyncio.to_thread(m.clear, m.preview(rule.id, [1]))
    assert not m.history(rule.id)["episodes"]


async def test_exact_url_orphan_merges_source_without_new_download(plugin, rule):
    from module.database import Database
    from module.models import RSSItem

    downloaded = await add_history(plugin, rule)
    async with Database() as db:
        rss = RSSItem(name="RSS", url="https://example.test/rss")
        db.add(rss)
        await db.commit()
        orphan = release()
        orphan.rss_id = rss.id
        db.add(orphan)
        await db.commit()
    result = await plugin.bridge.repair_orphans()
    assert result["merged"] == [{"removed": orphan.id, "kept": downloaded.id}]
    assert result["backup"]
    async with Database() as db:
        rows = await db.torrent.search_all()
        assert len(rows) == 1
        assert rows[0].downloaded and rows[0].rss_id == rss.id
    plugin.test_send.assert_not_called()


async def test_unfiltered_dedup_and_v2_priority(plugin, rule):
    t1 = release(1)
    t2 = release(1, url="magnet:?v2")
    t2.name = t2.name.replace("- 01 ", "- 01v2 ")
    for t in [t1, t2, release(2, "AVC")]:
        await plugin.bridge.capture(rule, t)
    await plugin.store.set_policy(rule.id, Policy(unfiltered=True, mode="backlog"), 0)
    result = await plugin.bridge.replay()
    assert result["sent"] == 2
    calls = str(plugin.test_send.call_args_list)
    assert "magnet:?v2" in calls and "dn=AVC" in calls
    assert "dn=HEVC" not in calls
    await plugin.bridge.replay()
    assert plugin.test_send.call_count == 2


def test_unfiltered_rejects_ambiguous_conditions():
    with pytest.raises(ValueError):
        Policy(unfiltered=True, include=["HEVC"]).validate()
    with pytest.raises(ValueError):
        Policy(unfiltered=True, version_id="anything").validate()


async def test_hibike_missing_resolution_and_season_episode_ambiguity(plugin, rule):
    from review_gate.versions import variants

    rule.official_title = "吹响吧！上低音号"
    rule.season = 3
    items = []
    for ep in (1, 3, 12, 13):
        name = f"[北宇治字幕组] 吹响吧！上低音号 第三季 / Hibike! Euphonium 3 [{ep:02}][WebRip][HEVC_AAC][简繁日内封][招募时轴]"
        parsed = plugin.bridge.parsed_data(rule, name)
        assert parsed["episode"] == ep
        assert json.loads(parsed["episode_key"])[-2:] == [3, ep]
        items.append({"name": name, **parsed})
    versions = variants(items)
    assert len(versions) == 1
    assert versions[0]["count"] == 4
    assert versions[0]["codec"] == "HEVC"
    assert versions[0]["resolution"] is None


async def test_bracket_v2_is_preferred_even_when_ab_parser_omits_revision(plugin, rule):
    rule.season = 3
    name = "[北宇治字幕组] 吹响吧！上低音号 第三季 / Hibike! Euphonium 3 [03v2][WebRip][HEVC_AAC][简繁日内封][招募时轴]"
    parsed = plugin.bridge.parsed_data(rule, name)
    assert parsed["episode"] == 3
    assert parsed["revision"] == 2
