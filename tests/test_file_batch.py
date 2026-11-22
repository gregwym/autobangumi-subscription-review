import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from review_gate.file_batch import decode_paths, episode_range
from review_gate.policy import Policy
from review_gate.versions import variants

NAME = "[Group] Example Show / 示例 [WebRip 1080p HEVC]"
FILES = [
    "[Group] Example Show - 01 [WebRip 1080p].mkv",
    "[Group] Example Show - 02 [WebRip 1080p].mkv",
]


def metainfo(paths):
    def string(s):
        data = s.encode()
        return str(len(data)).encode() + b":" + data

    files = b"".join(
        b"d6:lengthi1024e4:pathl" + b"".join(string(p) for p in path.split("/")) + b"ee"
        for path in paths
    )
    info = (
        b"d5:filesl" + files + b"e4:name4:demo12:piece lengthi16384e6:pieces20:" + b"x" * 20 + b"e"
    )
    return b"d4:info" + info + b"e", hashlib.sha1(info).hexdigest()


def test_metainfo_validates_infohash_and_reads_only_main_videos():
    data, digest = metainfo(FILES + ["SPs/Menu.mkv", "CDs/track.flac", "episode.ass"])
    assert decode_paths(data, digest) == FILES
    with pytest.raises(ValueError):
        decode_paths(data, "0" * 40)
    with pytest.raises(ValueError):
        decode_paths(data[:-2], digest)


@pytest.mark.parametrize(
    "paths",
    [
        FILES[:1],
        [FILES[0], FILES[1].replace("02", "03")],
        FILES + [FILES[0]],
        [FILES[0], FILES[1].replace("Example Show", "Different Show")],
        [FILES[0], "Season 2/" + FILES[1]],
        FILES + ["special.mkv"],
    ],
)
def test_inconclusive_contents_do_not_produce_a_batch(paths):
    rule = SimpleNamespace(official_title="Example Show", title_raw="Example Show", season=1)
    assert episode_range(paths, NAME, rule) is None


async def test_unnumbered_pack_is_selectable_and_obeys_existing_policy(plugin, rule, monkeypatch):
    from module.database import Database
    from module.models import RSSItem, Torrent
    from module.network import RequestContent

    from review_gate import file_batch

    data, digest = metainfo(FILES)
    url = f"https://mikanani.me/Download/20260101/{digest}.torrent"
    fetch = AsyncMock(return_value=decode_paths(data, digest))
    monkeypatch.setattr(file_batch, "fetch_paths", fetch)
    monkeypatch.setattr(RequestContent, "get_content", AsyncMock(return_value=data))
    async with Database() as db:
        rss = RSSItem(url="https://mikanani.me/RSS/Bangumi?bangumiId=123&subgroupid=987")
        db.add(rss)
        rule.rss_link = rss.url
        db.add(rule)
        await db.session.flush()
        db.add(Torrent(name=NAME, url=url, rss_id=rss.id, bangumi_id=rule.id))
        await db.commit()
    await plugin.bridge.sync_history()
    await plugin.store.set_policy(rule.id, Policy(unfiltered=True), 0)
    await plugin.bridge.replay()
    fetch.assert_awaited_once_with(url, digest)
    [entry] = await plugin.store.candidates(rule.id)
    [version] = variants([entry])
    assert version["kind"] == "batch" and version["ranges"] == [(1, 2)]
    assert entry["status"] == "held" and not plugin.test_send.called
    await plugin.store.set_policy(rule.id, Policy(version_ids=[version["id"]]), 1)
    await plugin.bridge.replay()
    await plugin.bridge.replay()
    fetch.assert_awaited_once()
    assert plugin.test_send.await_count == 1
