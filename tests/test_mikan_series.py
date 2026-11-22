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


async def test_duplicate_cards_and_review_only_candidates_get_backfilled_first(
    plugin, rule, monkeypatch
):
    from module.models import Bangumi

    series = plugin.bridge.mikan
    # Short unrelated rules used to consume the entire six-page budget.
    rules = {
        n: Bangumi(
            **(
                rule.model_dump()
                | {"id": n, "official_title": f"Show {n}" if n < 9 else "Duplicate"}
            )
        )
        for n in range(1, 11)
    }
    rows = []
    for n in rules:
        row = torrent(n, "fixture")
        row.bangumi_id = n
        rows.append(row)
    extra = torrent(20, "review only")
    seen = []

    async def fetch(page):
        seen.append(page)
        return 100

    from review_gate import mikan_series

    monkeypatch.setattr(mikan_series, "fetch_series", fetch)
    await series.observe(
        rules, rows, True, [{"rule_id": 9, "url": extra.url, "homepage": extra.homepage}]
    )
    assert set(seen[:3]) == {rows[8].homepage, rows[9].homepage, extra.homepage}
    assert len(seen) == 6 and extra.url not in series.owners


@pytest.mark.parametrize("same_series", [True, False])
async def test_cold_discovery_waits_for_the_series_behind_a_legacy_alias(
    plugin, rule, monkeypatch, same_series
):
    from module.database import Database
    from module.models import RSSItem
    from module.rss import RSSEngine
    from module.rss.analyser import RSSAnalyser

    from review_gate import mikan_series

    old = torrent(51, "[Group] Previous Title - 01 [1080p]")
    new = torrent(52, "[Group] Release Alias - 02 [1080p]")
    mapped = {old.homepage: 17, new.homepage: 17 if same_series else 23}
    monkeypatch.setattr(mikan_series, "fetch_series", AsyncMock(side_effect=mapped.__getitem__))

    async def enrich(_, bangumi, **kwargs):
        bangumi.official_title = "別の表示名"

    monkeypatch.setattr(RSSAnalyser, "official_title_parser", enrich)
    async with Database() as db:
        feed = RSSItem(url="https://example.test/reposted", aggregate=True)
        db.add(feed)
        rule.title_aliases = json.dumps(["Release Alias"])
        db.add(rule)
        old.bangumi_id = rule.id
        db.add(old)
        await db.commit()
        analyser = RSSAnalyser()
        engine = RSSEngine(db)
        await analyser.rss_to_data(feed, [new], engine, [rule])
        assert [r.id for r in await db.bangumi.search_all()] == [rule.id]
        assert engine.match_torrent(new, [rule]) is None
        await plugin.bridge.sync_history(inspect_files=True)
        await analyser.rss_to_data(feed, [new], engine, [rule])
        rules = await db.bangumi.search_all()
        assert len(rules) == (1 if same_series else 2)
        owner = engine.match_torrent(new, rules)
        assert owner and (owner.id == rule.id) == same_series
        assert plugin.bridge.mikan.identity(owner) == mapped[new.homepage]
    plugin.test_send.assert_not_called()


async def test_same_release_on_another_feed_never_creates_a_rule_during_backfill(
    plugin, rule, monkeypatch
):
    from module.database import Database
    from module.models import RSSItem
    from module.rss import RSSEngine
    from module.rss.analyser import RSSAnalyser

    from review_gate import mikan_series

    old = torrent(61, "[Group] Unrecorded Alias - 05 [1080p HEVC]")
    old.bangumi_id = rule.id
    old.downloaded = True
    incoming = old.model_copy(update={"id": None, "bangumi_id": None, "downloaded": False})
    monkeypatch.setattr(mikan_series, "fetch_series", AsyncMock(return_value=37))

    async def enrich(_, bangumi, **kwargs):
        bangumi.official_title = "別の表示名"

    monkeypatch.setattr(RSSAnalyser, "official_title_parser", enrich)
    async with Database() as db:
        feed = RSSItem(url="https://example.test/second", aggregate=True)
        db.add(feed)
        db.add(old)
        await db.commit()
        engine = RSSEngine(db)
        await RSSAnalyser().rss_to_data(feed, [incoming], engine, [rule])
        assert [r.id for r in await db.bangumi.search_all()] == [rule.id]
        assert plugin.bridge.mikan.identity(rule) is None
        assert (await db.torrent.search(old.id)).downloaded
    plugin.test_send.assert_not_called()


def test_unclassified_requires_a_complete_episode_page_for_the_expected_hash():
    key = "1" * 40
    page = f"""
        <div id="sk-container"><div class="episode-header"><p class="episode-title">Example</p></div>
        <p class="bangumi-info">Group</p><div class="leftbar-nav">
        <a class="episode-btn" href="/Download/20260101/{key}.torrent"></a></div></div>"""
    assert page_series(page, key) == 0
    assert page_series(page, "2" * 40) is None
    assert page_series(page.replace("episode-header", "other"), key) is None
    assert page_series(page + '<p class="bangumi-title"></p>', key) is None
    assert page_series(b"<html>Unavailable</html>", key) is None


async def test_unlisted_uses_exact_title_group_and_verified_episode_numbering(
    plugin, rule, monkeypatch
):
    from review_gate import mikan_series

    br = plugin.bridge
    gate = br.mikan
    rule.season = 2
    old = torrent(71, "[Group] Example Show - 18 [1080p]")
    old.bangumi_id = rule.id
    pack = torrent(72, "[Group] 示例番剧 / Example Show [18-22 修正合集][1080p HEVC][Fin]")
    from review_gate.mikan_series import episode_hash

    answers = {old.homepage: 17, pack.homepage: None}
    monkeypatch.setattr(mikan_series, "fetch_series", AsyncMock(side_effect=answers.__getitem__))
    await gate.resolve(old.homepage)
    await br.capture(rule, old)
    await gate.observe({rule.id: rule}, [old], False, await plugin.store.candidates())

    def match(item, rules):
        return gate.match(item, rules, br.match_rule, {})

    await gate.resolve(pack.homepage)
    assert match(pack, [rule]) is None
    key = episode_hash(pack.homepage)
    answers[pack.homepage] = 0
    gate.retry_at.clear()
    await gate.resolve(pack.homepage)
    assert key not in gate.pages and match(pack, [rule]).id == rule.id
    for bad in [
        pack.name.replace("18-22", "01-13"),
        pack.name.replace("Group", "Other"),
        pack.name.replace("示例番剧 / Example Show", "Example Show Extra"),
    ]:
        assert match(pack.model_copy(update={"name": bad}), [rule]) is None
    other = rule.model_copy(update={"id": rule.id + 1, "season": 1})
    await gate.observe(
        {rule.id: rule, other.id: other}, [old], False, await plugin.store.candidates()
    )
    assert match(pack, [rule]) is None
    # A subsequently classified release always follows its real series ID.
    answers[pack.homepage] = 23
    gate.unlisted_until[key] = 0
    await gate.resolve(pack.homepage)
    assert gate.pages[key] == 23 and match(pack, [rule]) is None
    plugin.test_send.assert_not_called()
