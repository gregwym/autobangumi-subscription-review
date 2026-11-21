import json
import sqlite3
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
