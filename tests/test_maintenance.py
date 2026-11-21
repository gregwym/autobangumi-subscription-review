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
        assert json.loads(parsed["episode_key"])[2:4] == [3, ep]
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


async def test_exact_group_prevents_cross_season_and_repairs_old_held_row(plugin):
    from module.database import Database
    from module.models import Bangumi, Torrent
    from module.rss.engine import RSSEngine

    async with Database() as db:
        one = Bangumi(
            official_title="青之箱",
            title_raw="Ao no Hako Blue Box",
            season=1,
            group_name="喵萌奶茶屋&LoliHouse",
            filter="",
        )
        two = Bangumi(
            official_title="青之箱",
            title_raw=one.title_raw,
            season=2,
            season_offset=-1,
            group_name="LoliHouse",
            filter="",
        )
        db.add(one)
        db.add(two)
        await db.commit()
        old = Torrent(
            name="[喵萌奶茶屋&LoliHouse] 青箱 / Ao no Hako / Blue Box - 04 [WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]",
            url="magnet:?old4",
            bangumi_id=two.id,
        )
        v2 = Torrent(
            name=old.name.replace("- 04 ", "- 04v2 "),
            url="magnet:?new4",
            bangumi_id=one.id,
            downloaded=True,
        )
        db.add(old)
        db.add(v2)
        await db.commit()
        assert RSSEngine(db).match_torrent(old.model_copy(), [two, one]).id == one.id
    await plugin.bridge.capture(two, old)
    await plugin.bridge.sync_history()
    assert not await plugin.store.candidates(two.id)
    assert {c["episode"] for c in await plugin.store.candidates(one.id)} == {4}
    async with Database() as db:
        assert (await db.torrent.search(old.id)).bangumi_id == one.id
        assert (await db.torrent.search(v2.id)).downloaded
    await plugin.bridge.sync_history()
    assert len(await plugin.store.candidates(one.id)) == 2
    plugin.test_send.assert_not_called()


async def test_ambiguous_season_stays_unmatched(plugin, rule):
    from module.rss.engine import RSSEngine

    newer = rule.model_copy(update={"id": 999, "season": 2})
    torrent = release()
    assert RSSEngine(None).match_torrent(torrent, [newer, rule]) is None
    assert torrent.bangumi_id is None


async def test_offset_edit_reindexes_history_and_retains_retry(plugin, rule):
    from module.database import Database

    await add_history(plugin, rule, 26)
    retry_id = await plugin.bridge.capture(rule, release(27))
    await plugin.store.reserve(await plugin.store.candidate(retry_id))
    await plugin.store.mark(retry_id, "retry")
    async with Database() as db:
        updated = await db.bangumi.search_id(rule.id)
        updated.episode_offset = -25
        updated.season_offset = 1
        db.add(updated)
        await db.commit()
    await plugin.bridge.sync_history()
    with sqlite3.connect(plugin.store.path) as db:
        keys = [json.loads(r[0]) for r in db.execute("SELECT episode_key FROM delivered")]
        reservations = [
            json.loads(r[0]) for r in db.execute("SELECT episode_key FROM reservations")
        ]
    assert [key[:4] for key in keys] == [[rule.official_title, "episode", 2, 1]]
    assert [key[:4] for key in reservations] == [[rule.official_title, "episode", 2, 2]]
    assert (await plugin.store.candidate(retry_id))["status"] == "retry"
    assert (await plugin.store.rules())[0]["state"] == "pending"
    assert [e["episode"] for e in plugin.bridge.maintenance().history(rule.id)["episodes"]] == [1]
    await plugin.bridge.sync_history()
    with sqlite3.connect(plugin.store.path) as db:
        assert db.execute("SELECT count(*) FROM delivered").fetchone()[0] == 1


async def test_corner_bracket_group_has_selectable_subtitle_variants(plugin, rule):
    from review_gate.versions import variants

    titles = [
        f"【今晚月色真美】[女友的朋友 / 彼女の友达 / Kanojo no Tomodachi][01v3][WebRip][1080P][AVC-8bit][AAC][{sub}][无修正]"
        for sub in ("简日内嵌", "繁日内嵌")
    ]
    items = [{"name": name, **plugin.bridge.parsed_data(rule, name)} for name in titles]
    result = variants(items)
    assert len(result) == 2
    assert {v["group"] for v in result} == {"今晚月色真美"}
    assert {v["subtitle"] for v in result} == {"简日内嵌", "繁日内嵌"}
    assert all(c["episode"] == 1 and c["revision"] == 3 for c in items)


async def test_removed_rule_filter_rediscovers_existing_orphan_without_download(plugin, rule):
    from module.database import Database

    torrent = release(2, "AVC")
    async with Database() as db:
        stored = await db.bangumi.search_id(rule.id)
        stored.filter = "AVC"
        db.add(stored)
        db.add(torrent)
        await db.commit()
    assert not (await plugin.bridge.repair_orphans())["repaired"]
    async with Database() as db:
        stored = await db.bangumi.search_id(rule.id)
        stored.filter = ""
        db.add(stored)
        await db.commit()
    assert (await plugin.bridge.repair_orphans())["repaired"] == [torrent.id]
    assert (await plugin.store.rules())[0]["state"] == "pending"
    assert len(await plugin.store.candidates(rule.id)) == 1
    await plugin.bridge.replay()
    plugin.test_send.assert_not_called()
