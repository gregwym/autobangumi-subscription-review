import json
import sqlite3
import time
from unittest.mock import AsyncMock

import pytest
from conftest import release

from review_gate.policy import Policy
from review_gate.versions import variants

PANTS = (
    "[LoliHouse] 一脸嫌弃表情的妹子给你看胖次R / 想看她一脸嫌恶地露出裤裤R / "
    "Iya na Kao Sare Nagara Opantsu Misete Moraitai Returns "
    "[01-06 合集][WebRip 1080p HEVC-10bit AAC][简繁内封字幕][Fin]"
)
FEATURE = (
    "[喵萌奶茶屋&LoliHouse] 超时空辉夜姬！ / Chou Kaguya-hime!{} "
    "[WebRip 1080p HEVC-10bit AAC][简繁内封字幕](检索用：超时空辉耀姬！)"
)
NEEDY = (
    "[LoliHouse] 主播女孩重度依赖 / NEEDY GIRL OVERDOSE "
    "[01-13话][WebRip 1080p HEVC-10bit AAC][简繁内封字幕][Fin]"
)
REZERO = (
    "[百冬练习组&LoliHouse] Re:从零开始的异世界生活 / "
    "Re:Zero kara Hajimeru Isekai Seikatsu - 81{} [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
)


def batch(url="magnet:?batch", revision=""):
    item = release(url=url)
    item.name = item.name.replace("- 01 ", f"- 01-06{revision} ")
    return item


async def preview_and_approve(api, rid, ids):
    path = f"/rules/{rid}"
    preview = await api.post(f"{path}/preview", json={"version_ids": ids})
    assert preview.status_code == 200, preview.text
    response = await api.post(
        f"{path}/approve", json={"preview_token": preview.json()["preview_token"]}
    )
    assert response.status_code == 200, response.text
    return preview.json()


@pytest.mark.parametrize("chosen", [{"single"}, {"batch"}, {"single", "batch"}])
async def test_select_single_and_batch_independently(plugin, rule, chosen):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from module.conf import settings

    settings.rss_parser.filter = []
    for item in (release(url="magnet:?single"), batch(), batch("magnet:?batch-v2", "v2")):
        await plugin.bridge.capture(rule, item)
    app = FastAPI()
    app.include_router(plugin.api())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        options = (await api.get(f"/rules/{rule.id}/versions")).json()["versions"]
        assert {v["kind"] for v in options} == {"single", "batch"}
        assert [v["ranges"] for v in options if v["kind"] == "batch"] == [[[1, 6]]]
        preview = await preview_and_approve(
            api, rule.id, [v["id"] for v in options if v["kind"] in chosen]
        )
    assert preview["will_download"] == len(chosen)
    await plugin.bridge.replay()
    assert plugin.test_send.call_count == len(chosen)
    sent = [call.args[0] for call in plugin.test_send.call_args_list]
    assert ("magnet:?single" in sent) == ("single" in chosen)
    assert ("magnet:?batch-v2" in sent) == ("batch" in chosen)
    assert "magnet:?batch" not in sent


@pytest.mark.parametrize("previous_batch", [False, True])
async def test_legacy_history_keeps_only_same_version_dedup(plugin, rule, previous_batch):
    from module.conf import settings
    from module.database import Database

    settings.rss_parser.filter = []
    old = batch(url="magnet:?old") if previous_batch else release(url="magnet:?old")
    old.bangumi_id = rule.id
    old.downloaded = True
    async with Database() as db:
        db.add(old)
        await db.commit()
    await plugin.bridge.capture(rule, old)
    [record] = await plugin.store.candidates(rule.id)
    modern_key = json.loads(record["episode_key"])
    with sqlite3.connect(plugin.store.path) as db:
        db.execute(
            "INSERT INTO delivered VALUES(?,?)",
            (json.dumps(modern_key[:4], ensure_ascii=False), old.url),
        )
    foreign = release(url="magnet:?foreign")
    foreign.name = foreign.name.replace("[Group]", "[Different]")
    for item in (release(url="magnet:?single"), batch(), foreign):
        await plugin.bridge.capture(rule, item)
    await plugin.bridge.sync_history()
    options = variants(await plugin.store.candidates(rule.id))
    await plugin.store.set_policy(rule.id, Policy(version_ids=[v["id"] for v in options]), 0)
    await plugin.bridge.replay()
    sent = [call.args[0] for call in plugin.test_send.call_args_list]
    expected = "magnet:?single" if previous_batch else "magnet:?batch"
    assert set(sent) == {expected, "magnet:?foreign"}
    assert len(sent) == 2
    assert {r["label"] for r in plugin.bridge.maintenance().history(rule.id)["episodes"]} == {
        "第 1 集",
        "合集 1–6 集",
    }


async def test_legacy_uncertain_send_retries_its_url_even_with_v2(plugin, rule):
    source = release(url="magnet:?original")
    await plugin.bridge.capture(rule, source)
    [record] = await plugin.store.candidates(rule.id)
    with sqlite3.connect(plugin.store.path) as db:
        key = json.dumps(json.loads(record["episode_key"])[:4], ensure_ascii=False)
        db.execute("INSERT INTO reservations VALUES(?,?)", (key, source.url))
    await plugin.store.mark(record["id"], "retry")
    incoming = release(url="magnet:?newer")
    incoming.name = incoming.name.replace("- 01 ", "- 01v2 ")
    await plugin.bridge.capture(rule, incoming)
    await plugin.bridge.sync_history()
    await plugin.store.set_policy(rule.id, Policy(unfiltered=True), 0)
    await plugin.bridge.replay()
    assert [call.args[0] for call in plugin.test_send.call_args_list] == ["magnet:?original"]


@pytest.mark.parametrize("aggregate", [False, True])
async def test_real_batch_title_through_ab_rss_path(plugin, monkeypatch, aggregate):
    from module.conf import settings
    from module.database import Database
    from module.downloader import DownloaderPool
    from module.models import RSSItem, Torrent
    from module.rss import RSSAnalyser, RSSEngine

    settings.rss_parser.filter = []
    item = Torrent(name=PANTS, url="magnet:?real-pack")
    rss = RSSItem(
        name="Real batch sample",
        url="https://mikanani.me/RSS/Bangumi?bangumiId=3969&subgroupid=370",
        aggregate=aggregate,
        parser="mikan",
    )
    monkeypatch.setattr(RSSAnalyser, "official_title_parser", AsyncMock())
    monkeypatch.setattr(RSSAnalyser, "get_rss_torrents", AsyncMock(return_value=[item]))

    async def pull(source):
        return [item.model_copy(update={"rss_id": source.id})]

    monkeypatch.setattr(RSSEngine, "_get_torrents", staticmethod(pull))
    async with Database() as db:
        db.add(rss)
        if not aggregate:
            rule = await RSSAnalyser().link_to_data(rss)
            assert rule.group_name == "LoliHouse"
            db.add(rule)
        await db.commit()
    async with Database() as db, DownloaderPool() as pool:
        await RSSEngine(db).refresh_rss(pool, analyser=RSSAnalyser())
        [rule] = await db.bangumi.search_all()
        assert not await db.torrent.search_orphans()
    plugin.test_send.assert_not_called()
    [option] = variants(await plugin.store.candidates(rule.id))
    assert option["ranges"] == [(1, 6)]
    await plugin.store.set_policy(rule.id, Policy(version_ids=[option["id"]]), 0)
    await plugin.bridge.replay()
    assert [call.args[0] for call in plugin.test_send.call_args_list] == ["magnet:?real-pack"]


async def test_orphan_pack_respects_both_filters(plugin, rule):
    from module.conf import settings
    from module.database import Database

    item = batch()
    async with Database() as db:
        db.add(item)
        await db.commit()
    settings.rss_parser.filter = [r"\d+-\d+"]
    assert not (await plugin.bridge.repair_orphans())["repaired"]
    settings.rss_parser.filter = []
    async with Database() as db:
        current = await db.bangumi.search_id(rule.id)
        current.filter = r"\d+-\d+"
        db.add(current)
        await db.commit()
    assert not (await plugin.bridge.repair_orphans())["repaired"]
    async with Database() as db:
        current = await db.bangumi.search_id(rule.id)
        current.filter = ""
        db.add(current)
        await db.commit()
    assert (await plugin.bridge.repair_orphans())["repaired"] == [item.id]
    assert len(await plugin.store.candidates(rule.id)) == 1
    plugin.test_send.assert_not_called()


async def test_excluded_revision_does_not_block_older_permitted_batch(plugin, rule):
    from module.conf import settings

    settings.rss_parser.filter = [r"v2\b"]
    for item in (batch(), batch("magnet:?excluded", "v2")):
        await plugin.bridge.capture(rule, item)
    [option] = variants(await plugin.store.candidates(rule.id))
    await plugin.store.set_policy(rule.id, Policy(version_ids=[option["id"]]), 0)
    await plugin.bridge.replay()
    assert [call.args[0] for call in plugin.test_send.call_args_list] == ["magnet:?batch"]


@pytest.mark.parametrize(
    "suffix",
    ["S01+S02 [01-12]", "S01-S03 [01-12]", "Complete", "[01-06][OP+ED]", "[01-06]+[13-18]"],
)
def test_ambiguous_batch_has_no_identity(plugin, rule, suffix):
    parsed = plugin.bridge.parsed_data(rule, f"[Group] Example Show {suffix} [1080p HEVC]")
    assert not parsed["episode_key"]


@pytest.mark.parametrize("dedicated", [False, True])
async def test_mikan_revision_pair_uses_saved_id_and_sends_v2_once(plugin, monkeypatch, dedicated):
    from module.database import Database
    from module.models import Bangumi, RSSItem, Torrent
    from module.network import RequestContent

    url = (
        "https://mikanani.me/RSS/Bangumi?bangumiId=3878&subgroupid=370"
        if dedicated
        else "https://mikanani.me/RSS/MyBangumi?token=fixture"
    )
    async with Database() as db:
        rss = RSSItem(url=url)
        rule = Bangumi(
            official_title="超辉夜姬！",
            title_raw="Chou Kaguya-hime!",
            group_name="喵萌奶茶屋&LoliHouse",
            rss_link=url,
            filter="",
        )
        db.add(rss)
        db.add(rule)
        await db.session.flush()
        for revision in ("", " v2"):
            edition = 2 if revision else 1
            item = Torrent(
                name=FEATURE.format(revision),
                url=f"https://mikanani.me/Download/{edition}.torrent",
                rss_id=rss.id,
                bangumi_id=rule.id,
            )
            db.add(item)
            await plugin.bridge.capture(rule, item)
        await db.commit()
    # The same ID from the installed plugin's saved approval, before it had a key.
    await plugin.store.set_policy(rule.id, Policy(version_ids=["a5088bd1b3ba074ff6bf"]), 0)
    monkeypatch.setattr(RequestContent, "get_content", AsyncMock(return_value=b"fixture"))
    await plugin.bridge.replay()
    entries = await plugin.store.candidates(rule.id)
    assert all(item["episode"] is None for item in entries)
    if dedicated:
        [version] = variants(entries)
        assert version["kind"] == "feature" and version["id"] == "a5088bd1b3ba074ff6bf"
        assert {c["revision"]: c["status"] for c in entries} == {1: "held", 2: "delivered"}
        assert plugin.bridge.maintenance().history(rule.id)["episodes"][0]["label"] == "单片"
        await plugin.bridge.replay()
        assert plugin.test_send.call_count == 1
    else:
        assert not variants(entries)
        plugin.test_send.assert_not_called()


@pytest.mark.parametrize("engine", ["tokenizer", "classic"])
@pytest.mark.parametrize(("season", "chinese"), [(2, "二"), (4, "四")])
def test_equivalent_bilingual_seasons_do_not_look_like_multiple_seasons(
    plugin, rule, engine, season, chinese
):
    from module.conf import settings

    settings.rss_parser.engine = engine
    rule.season = season
    other = rule.model_copy(update={"id": 999, "season": season + 1})
    title = f"[Group] 示例第{chinese}季 / Example Show S{season} [01-12 合集][1080p HEVC]"
    assert plugin.bridge.match_rule(title, [rule, other]) is rule
    assert plugin.bridge.parsed_data(rule, title)["episode_end"] == 12
    unbracketed = title.replace("[01-12 合集]", "- 01-12 ")
    assert plugin.bridge.match_rule(unbracketed, [rule, other]) is rule
    mismatched = title.replace(f"S{season}", f"S{season + 1}")
    assert plugin.bridge.match_rule(mismatched, [rule, other]) is None
    assert not plugin.bridge.parsed_data(rule, mismatched)["episode_key"]


async def test_labeled_range_reindexes_v050_history_without_consuming_the_last_episode(plugin):
    from module.conf import settings
    from module.database import Database
    from module.models import Bangumi, Torrent

    from review_gate.versions import version_traits

    settings.rss_parser.filter = []
    async with Database() as db:
        rule = Bangumi(
            official_title="主播女孩重度依赖", title_raw="NEEDY GIRL OVERDOSE", filter=""
        )
        db.add(rule)
        await db.session.flush()
        old = Torrent(
            name=NEEDY, url="magnet:?pack-already-downloaded", downloaded=True, bangumi_id=rule.id
        )
        db.add(old)
        await db.commit()
    # v0.5.0 persisted "[01-13话]" as single episode 13.
    legacy = {
        "episode": 13,
        "episode_end": None,
        "revision": 1,
        "group": "LoliHouse",
        "resolution": "1080p",
        "source": "WebRip",
        "subtitle": "简繁内封字幕",
    }
    single_version = version_traits({**legacy, "name": NEEDY})[0]
    legacy["episode_key"] = json.dumps(
        [rule.official_title, "episode", 1, 13, single_version, None], ensure_ascii=False
    )
    await plugin.store.seed(plugin.bridge.rule_data(rule), plugin.bridge.torrent_data(old), legacy)
    await plugin.bridge.sync_history()
    [option] = variants(await plugin.store.candidates(rule.id))
    assert option["kind"] == "batch" and option["ranges"] == [(1, 13)]
    with sqlite3.connect(plugin.store.path) as db:
        [row] = db.execute("SELECT episode_key FROM delivered").fetchall()
    assert json.loads(row[0])[3:] == [1, option["id"], 13]
    normal = Torrent(name=NEEDY.replace("[01-13话]", "- 13"), url="magnet:?single-13")
    mirror = Torrent(name=NEEDY.replace("[01-13话]", "[01-13]"), url="magnet:?pack-mirror")
    await plugin.bridge.capture(rule, normal)
    await plugin.bridge.capture(rule, mirror)
    await plugin.store.set_policy(rule.id, Policy(version_ids=[single_version, option["id"]]), 0)
    await plugin.bridge.replay()
    assert [c.args[0] for c in plugin.test_send.call_args_list] == ["magnet:?single-13"]


@pytest.mark.parametrize("v1_downloaded", [False, True])
async def test_spaced_revision_rejoins_the_same_rule_and_uses_history(plugin, v1_downloaded):
    from module.conf import settings
    from module.database import Database
    from module.models import Bangumi, Torrent

    settings.rss_parser.filter = []
    original = Torrent(
        name=REZERO.format(""), url="magnet:?rezero-original", downloaded=v1_downloaded
    )
    v2 = Torrent(name=REZERO.format(" v2"), url="magnet:?rezero-v2")
    async with Database() as db:
        rule = Bangumi(
            official_title="Re:从零开始的异世界生活",
            title_raw="Re: Re:Zero kara Hajimeru Isekai Seikatsu",
            group_name="百冬练习组&LoliHouse",
            season=4,
            episode_offset=-66,
            filter="",
        )
        db.add(rule)
        await db.session.flush()
        original.bangumi_id = rule.id
        db.add(original)
        db.add(v2)
        await db.commit()
    await plugin.bridge.capture(rule, original)
    [option] = variants(await plugin.store.candidates(rule.id))
    await plugin.store.set_policy(rule.id, Policy(version_ids=[option["id"]]), 0)
    await plugin.bridge.replay()
    candidates = await plugin.store.candidates(rule.id)
    assert {c["revision"] for c in candidates} == {1, 2}
    assert len({c["episode_key"] for c in candidates}) == 1
    assert json.loads(candidates[0]["episode_key"])[2:4] == [4, 15]
    assert {c["name"] for c in candidates} == {original.name, v2.name}
    [policy] = await plugin.store.rules(rule.id)
    assert policy["state"] == "approved" and policy["policy"]["version_ids"] == [option["id"]]
    assert [c.args[0] for c in plugin.test_send.call_args_list] == (
        [] if v1_downloaded else ["magnet:?rezero-v2"]
    )


@pytest.mark.parametrize("exclusion", ["global", "rule"])
@pytest.mark.parametrize("name, pattern", [(NEEDY, r"13话"), (REZERO.format(" v2"), r"81\s+v2")])
async def test_orphan_reports_filter_for_corrected_release_without_normalizing_regex(
    plugin, rule, name, pattern, exclusion
):
    from module.conf import settings
    from module.database import Database
    from module.models import Torrent

    settings.rss_parser.filter = [pattern] if exclusion == "global" else []
    async with Database() as db:
        current = await db.bangumi.search_id(rule.id)
        current.title_raw = (
            "NEEDY GIRL OVERDOSE" if name == NEEDY else "Re: Re:Zero kara Hajimeru Isekai Seikatsu"
        )
        current.filter = pattern if exclusion == "rule" else ""
        db.add(current)
        db.add(Torrent(name=name, url="magnet:?fixture"))
        await db.commit()
    [row] = await plugin.bridge.orphan_records()
    assert row["name"] == name and row["rule_id"] == rule.id and not row["repairable"]
    assert row["reason"] == (
        "被 AB 全局过滤条件排除" if exclusion == "global" else "被 AB 单番过滤条件排除"
    )
    assert not (await plugin.bridge.repair_orphans())["repaired"]
    plugin.test_send.assert_not_called()


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("mode", ["backlog", "future"])
async def test_orphan_repair_obeys_existing_approval_and_start_mode(plugin, rule, manual, mode):
    from module.database import Database

    item = release(url="magnet:?previously-seen-by-ab")
    async with Database() as db:
        db.add(item)
        await db.commit()
    await plugin.store.register(plugin.bridge.rule_data(rule))
    await plugin.store.set_policy(
        rule.id, Policy(unfiltered=True, mode=mode, cutoff=time.time()), 0
    )
    result = await plugin.bridge.repair_orphans({item.id} if manual else None)
    assert result["repaired"] == [item.id]
    [policy] = await plugin.store.rules(rule.id)
    assert policy["state"] == ("pending" if manual else "approved")
    assert policy["policy"]["mode"] == mode
    await plugin.bridge.replay()
    assert plugin.test_send.call_count == int(not manual and mode == "backlog")


@pytest.mark.parametrize("label", ["合集", "合輯", "合辑"])
def test_bracketed_collection_suffix_with_a_number_in_the_title(label):
    from review_gate.batch import parsed_batch

    name = f"[Group] 示例 0/Example Show 0 [01-24{label}][WebRip 1080p HEVC][Fin]"
    release = parsed_batch(name)
    assert release and (release.episode, release.episode_end) == (1, 24)
    assert release.raw == name
    assert parsed_batch(name.replace("01-24", "24-01")) is None
    assert parsed_batch(name + "[NCOP]") is None


async def test_filter_accepts_readonly_batch_view_only_after_version_approval(plugin, rule):
    from module.conf import settings
    from module.models import Torrent
    from module.plugin.views import bangumi_info, torrent_info

    settings.rss_parser.filter = ["720"]
    item = Torrent(name="[Group] Example Show [14-22][WebRip 1080p HEVC]", url="magnet:?batch-view")
    resource, owner = torrent_info(item), bangumi_info(rule)
    assert not (await plugin.review(resource, None, owner)).accept
    [version] = variants(await plugin.store.candidates(rule.id))
    assert version["ranges"] == [(14, 22)]
    [stored] = await plugin.store.rules(rule.id)
    await plugin.store.set_policy(rule.id, Policy(version_ids=[version["id"]]), stored["revision"])
    assert (await plugin.review(resource, None, owner)).accept
    plugin.test_send.assert_not_called()
