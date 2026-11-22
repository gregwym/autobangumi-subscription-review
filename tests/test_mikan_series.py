import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from review_gate.batch import parse_review_release
from review_gate.file_batch import episode_range, numbered_title_aliases
from review_gate.mikan_series import page_series, release_page
from review_gate.policy import Policy
from review_gate.versions import variants


def torrent(number, name):
    from module.models import Torrent

    key = f"{number:040x}"
    return Torrent(
        name=name,
        url=f"https://mikanani.me/Download/20260101/{key}.torrent",
        homepage=f"https://mikanani.me/Home/Episode/{key}",
    )


def test_series_uses_only_the_series_link_for_the_actual_release():
    def page(*links):
        return (
            '<a href="/Home/Bangumi/5"></a><p class="bangumi-title">'
            + "".join(f'<a href="{x}"></a>' for x in links)
            + "</p>"
        ).encode()

    assert page_series(page("/Home/Bangumi/37#28")) == 37
    assert page_series(page("/Home/Bangumi/37#28", "/Home/Bangumi/37#1")) == 37
    assert page_series(page("/Home/Bangumi/37", "/Home/Bangumi/51")) is None
    assert page_series(page("https://example.test/Home/Bangumi/37")) is None
    a, b = torrent(1, "a"), torrent(2, "b")
    assert release_page(a.url, a.homepage) == a.homepage
    assert release_page(a.url, b.homepage) is None


def test_number_in_title_needs_files():
    from review_gate.bridge40 import Bridge40

    rule = SimpleNamespace(
        id=1, title_raw="demo", official_title="示例 2", season=1, episode_type="episode"
    )
    name = "【悠哈璃羽字幕社】[示例 2/别名 2_demo 2][BDRIP 1920x1080 x264 AAC][MKV 简繁外挂]"
    files = [
        f"[GROUP][demo 2][{mark}][BDRIP 1920x1080 x264 AAC].mkv"
        for mark in ("Collection", "01", "02", "03")
    ]
    release = parse_review_release(name)[1]
    assert release.episode == 2
    aliases = numbered_title_aliases(name, rule, release)
    assert Bridge40.parsed_data(rule, name, inspect_title=True)["episode_key"] is None
    assert episode_range(files, name, rule, numbered_aliases=aliases) == (1, 3)
    assert episode_range(files[1:2], name, rule, numbered_aliases=aliases) == (1, 1)
    assert episode_range(files[:2], name, rule, numbered_aliases=aliases) is None
    assert (
        episode_range([files[0], files[1], files[3]], name, rule, numbered_aliases=aliases) is None
    )
    assert (
        episode_range(
            [files[0].replace("demo 2", "other 2"), *files[1:]],
            name,
            rule,
            numbered_aliases=aliases,
        )
        is None
    )


async def test_discovery_by_metadata_across_feeds_and_with_same_display_name(plugin, monkeypatch):
    from module.database import Database
    from module.models import RSSItem
    from module.rss import RSSEngine
    from module.rss.analyser import RSSAnalyser

    from review_gate import mikan_series

    first = torrent(1, "[Group] Example Show - 01 [1080p HEVC]")
    other = torrent(2, "[Other] An Alias - 01 [1080p x264]")
    different = torrent(3, first.name)
    mapping = {first.homepage: 17, other.homepage: 17, different.homepage: 23}
    monkeypatch.setattr(
        mikan_series, "fetch_series", AsyncMock(side_effect=lambda page: mapping[page])
    )

    async def enrich(self, bangumi, *args, **kwargs):
        bangumi.official_title = "同名作品"

    monkeypatch.setattr(RSSAnalyser, "official_title_parser", enrich)
    async with Database() as db:
        feeds = [RSSItem(url=f"https://example.test/{n}", aggregate=True) for n in range(2)]
        for feed in feeds:
            db.add(feed)
        await db.commit()
        engine = RSSEngine(db)
        analyser = RSSAnalyser()
        for release, feed in ((first, feeds[0]), (other, feeds[1]), (different, feeds[0])):
            release.rss_id = feed.id
            await analyser.rss_to_data(feed, [release], engine, await db.bangumi.search_all())
            owner = engine.match_torrent(release, await db.bangumi.search_all())
            assert owner
            db.add(release)
            await db.commit()
        assert first.bangumi_id == other.bangumi_id != different.bangumi_id
        rules = await db.bangumi.search_all()
        assert len(rules) == 2
        common = next(r for r in rules if r.id == first.bangumi_id)
        assert json.loads(common.title_aliases) == ["An Alias"]
        assert set(common.rss_link.split(",")) == {r.url for r in feeds}
    await plugin.bridge.sync_history()
    a, b = await plugin.store.candidates(common.id)
    assert {v["codec"] for v in variants([a, b])} == {"HEVC", "x264"}
    separate = (await plugin.store.candidates(different.bangumi_id))[0]
    assert (
        a["episode_key"] != separate["episode_key"]
        and json.loads(separate["episode_key"])[-1] == 23
    )
    plugin.test_send.assert_not_called()


@pytest.mark.parametrize("protected", [True, False])
async def test_merge_verified_pending_siblings_preserves_versions_and_scope(
    plugin, rule, monkeypatch, protected
):
    from module.database import Database
    from module.models import Bangumi

    from review_gate import mikan_series

    a = torrent(10, "[Group] Example Show - 01 [1080p HEVC]")
    b = torrent(11, "[Group] Example Alias - 01 [1080p x264]")
    monkeypatch.setattr(mikan_series, "fetch_series", AsyncMock(return_value=123))
    async with Database() as db:
        second = Bangumi(
            **(
                rule.model_dump(exclude={"id"})
                | {"title_raw": "Example Alias", "rss_link": "https://example.test/b"}
            )
        )
        db.add(second)
        await db.session.flush()
        a.bangumi_id, b.bangumi_id = rule.id, second.id
        db.add(a)
        db.add(b)
        await db.commit()
    await plugin.bridge.capture(second, a, first_seen=0)
    await plugin.bridge.sync_history(inspect_files=True)
    assert len(await plugin.store.candidates(second.id)) == 1
    m = plugin.bridge.maintenance()
    assert m.merge_options(rule.id) == [second.id]
    if protected:
        await plugin.store.set_policy(second.id, Policy(unfiltered=True), 0)
        with pytest.raises(ValueError):
            m.merge_mikan(rule.id, second.id)
        await plugin.store.hold(second.id)
        assert m.merge_options(rule.id) == []
        return
    result = m.merge_mikan(rule.id, second.id)
    assert result["merged"] == second.id
    await plugin.bridge.sync_history()
    assert not m.merge_options(rule.id)
    assert {v["codec"] for v in variants(await plugin.store.candidates(rule.id))} == {
        "x264",
        "HEVC",
    }
    assert not await plugin.store.candidates(second.id)
    async with Database() as db:
        assert (await db.bangumi.search_id(second.id)).deleted
        assert {t.bangumi_id for t in await db.torrent.search_all()} == {rule.id}
    plugin.test_send.assert_not_called()
