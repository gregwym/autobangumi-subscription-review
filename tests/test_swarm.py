import asyncio
import base64
import struct
from unittest.mock import AsyncMock

import pytest

from review_gate import swarm
from review_gate.opentrackr import parse_response
from review_gate.swarm import SwarmHealth, index_page, magnet_hash, summarize


def torrent(n, name):
    from module.models import Torrent

    h = f"{n:040x}"
    return Torrent(
        name=name,
        url=f"https://mikanani.me/Download/20260101/{h}.torrent",
        homepage=f"https://mikanani.me/Home/Episode/{h}",
    )


def test_index_exact_hash_base32_unknown_and_pagination():
    h = "ed" * 20
    alt = base64.b32encode(bytes.fromhex(h)).decode()
    html = f"""<table id="topic_list"><thead><tr><th>磁鏈</th><th>下載</th><th>種子</th></tr></thead><tbody>
      <tr><td><a href="magnet:?xt=urn:btih:{alt}"></a></td><td>-</td><td>0</td></tr>
      <tr><td><a href="magnet:?xt=urn:btih:{"ab" * 20}"></a></td><td>21</td><td>-</td></tr>
      </tbody></table><div class="nav_title"><a href="/topics/list/page/2?keyword=x">下一頁</a></div>"""
    assert index_page(html, 1) == ({h: {"seeds": 0, "leechers": None}}, True)
    assert index_page(html, 2)[1] is False
    assert magnet_hash(f"magnet:?xt=urn:btih:{alt}&xt=urn:btih:{'ab' * 20}") is None
    assert index_page("upstream error", 1) == ({}, False)


def test_tracker_requires_matching_response_and_complete_counts():
    data = struct.pack("!IIIII", 2, 44, 7, 15, 9)
    assert struct.unpack("!III", parse_response(data, 2, 44, 12)) == (7, 15, 9)
    for invalid in [
        data[:-1],
        struct.pack("!II", 3, 44) + b"not available",
        struct.pack("!II", 2, 45) + data[8:],
    ]:
        with pytest.raises(ValueError):
            parse_response(invalid, 2, 44, 12)


def test_range_groups_revisions_and_distinguishes_zero_and_unknown():
    def row(ep, *counts):
        return {"episode": ep, "reports": [{"seeds": count} for count in counts]}

    rows = [row(1, 1, 3), row(1, 7), row(2, 0), row(3), row(4, 4)]
    assert summarize(rows, single=True) == {"min": 0, "max": 7, "total": 4, "zero": 1, "unknown": 1}
    assert summarize(rows[:2], single=False) == {
        "min": 3,
        "max": 7,
        "total": 2,
        "zero": 0,
        "unknown": 0,
    }
    assert summarize([row(3)], single=True)["min"] is None


async def test_version_health_is_read_only_exact_and_cached(plugin, rule, api, monkeypatch):
    pack = torrent(11, "[Group] Example Show [01-02 合集][1080p HEVC]")
    single = torrent(12, "[Group] Example Show - 01 [1080p HEVC]")
    other = torrent(13, "[Group] Example Show - 02 [1080p HEVC]")
    private = single.model_copy(
        update={"url": "https://private.test/rss?passkey=secret", "homepage": None}
    )
    for t in [pack, single, other, private]:
        await plugin.bridge.capture(rule, t)
    h1, h2, h3 = [f"{n:040x}" for n in (11, 12, 13)]

    def index(term, page):
        assert "secret" not in term
        if page == 1:
            return {
                "a" * 40: {"seeds": 5000, "leechers": 21},
                h2: {"seeds": 0, "leechers": 3},
            }, True
        return {h1: {"seeds": 2, "leechers": 0}}, False

    lookup = AsyncMock(side_effect=index)
    tracker = AsyncMock(
        return_value={
            h1: {"seeds": 5, "leechers": 1},
            h2: {"seeds": 3, "leechers": 2},
            h3: {"seeds": 8, "leechers": 4},
        }
    )
    monkeypatch.setattr(swarm, "fetch_index", lookup)
    monkeypatch.setattr(swarm, "scrape", tracker)
    monkeypatch.setattr(swarm, "getproxies_environment", lambda: {})
    async with api:
        path = f"/review/rules/{rule.id}"
        version_response = (await api.get(path + "/versions")).json()
        kinds = {v["kind"]: v["id"] for v in version_response["versions"]}
        before = (await api.post(path + "/preview", json={"version_ids": [kinds["batch"]]})).json()
        responses = await asyncio.gather(api.get(path + "/swarm"), api.get(path + "/swarm"))
        assert all(
            r.status_code == 200 and "secret" not in r.text and h1 not in r.text for r in responses
        )
        a = responses[0].json()
        [p] = a[kinds["batch"]]["releases"]
        assert [(r["source"], r["seeds"]) for r in p["reports"]] == [
            ("动漫花园", 2),
            ("OpenTrackr", 5),
        ]
        assert a[kinds["single"]]["summary"] == {
            "min": 3,
            "max": 8,
            "total": 2,
            "unknown": 0,
            "zero": 0,
        }
        assert any(not r["reports"] for r in a[kinds["single"]]["releases"])
        assert responses[1].json() == a
        tracker.assert_awaited_once_with([h1, h2, h3])
        assert lookup.await_count == 2
        assert (await api.get(path + "/versions")).json() == version_response
        assert (
            await api.post(path + "/approve", json={"preview_token": before["preview_token"]})
        ).status_code == 200
    plugin.test_send.assert_not_called()


async def test_source_outage_and_proxy_do_not_generate_zero_reports(monkeypatch):
    from module.conf import settings

    key = "ab" * 20
    service = SwarmHealth()
    lookup = AsyncMock(side_effect=TimeoutError)
    tracker = AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr(swarm, "fetch_index", lookup)
    monkeypatch.setattr(swarm, "scrape", tracker)
    monkeypatch.setattr(swarm, "getproxies_environment", lambda: {"https": "proxy"})
    assert await service.tracker_stats({key}) == {}
    tracker.assert_not_called()
    monkeypatch.setattr(swarm, "getproxies_environment", lambda: {})
    monkeypatch.setattr(settings.proxy, "enable", True)
    assert await service.tracker_stats({key}) == {}
    tracker.assert_not_called()
    monkeypatch.setattr(settings.proxy, "enable", False)
    for _ in range(2):
        assert await service.tracker_stats({key}) == {}
        assert await service.index_stats({"Example": {key}}) == {}
    tracker.assert_awaited_once()
    lookup.assert_awaited_once()
