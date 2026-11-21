"""Confirm revision-paired single features in dedicated Mikan feeds."""

import re
import unicodedata
from collections import defaultdict
from urllib.parse import parse_qs, urlsplit

from .batch import parse_review_release

MIKAN_HOSTS = {"mikanani.me", "mikanime.tv"}
REVISION = re.compile(r"(?<!\w)v([1-9]\d*)(?!\w)", re.I)


def dedicated_feed(url):
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in MIKAN_HOSTS
        or parsed.path.casefold() != "/rss/bangumi"
    ):
        return None
    query = parse_qs(parsed.query)
    values = [query.get(key, []) for key in ("bangumiId", "subgroupid")]
    if all(len(value) == 1 and re.fullmatch(r"[0-9]{1,10}", value[0]) for value in values):
        return tuple(int(value[0]) for value in values)
    return None


def normalized_title(title):
    return "".join(c for c in unicodedata.normalize("NFKC", title or "").casefold() if c.isalnum())


def confirmed_features(rules, torrents, sources):
    from module.database.bangumi import _all_title_patterns, _normalize_group_name
    from module.parser.analyser.tokenizer.result import MediaType, ReleaseKind

    subscriptions = {
        rid: {feed for link in (rule.rss_link or "").split(",") if (feed := dedicated_feed(link))}
        for rid, rule in rules.items()
    }
    numbered = set()
    seen = set()
    pairs = defaultdict(list)
    for rid, torrent in torrents:
        feeds = subscriptions.get(rid)
        if not feeds:
            continue
        key = (rid, torrent["url"], torrent["name"], torrent.get("rss_id"))
        if key in seen:
            continue
        seen.add(key)
        _, release = parse_review_release(torrent["name"])
        if release is None:
            continue
        if release.episode is not None or release.release_kind is not ReleaseKind.SINGLE:
            numbered.add(rid)
            continue
        try:
            url_host = urlsplit(torrent["url"]).hostname
        except ValueError:
            continue
        if url_host not in MIKAN_HOSTS:
            continue
        feed = sources.get(torrent.get("rss_id"))
        if feed not in feeds:
            # Older imports can omit rss_id. Only recover when the rule has one
            # dedicated feed and the torrent itself comes from Mikan.
            if torrent.get("rss_id") is not None or len(feeds) != 1:
                continue
            feed = next(iter(feeds))
        rule = rules[rid]
        if (
            release.media_type not in {MediaType.UNKNOWN, MediaType.MOVIE}
            or release.season is not None
            or not release.group
            or not release.resolution
            or (
                rule.group_name
                and _normalize_group_name(rule.group_name) != _normalize_group_name(release.group)
            )
        ):
            continue
        titles = {
            normalized_title(title) for title in (*_all_title_patterns(rule), rule.official_title)
        }
        primary = normalized_title(release.primary_title)
        if len(primary) < 4 or primary not in titles:
            continue
        raw = unicodedata.normalize("NFKC", torrent["name"])
        revisions = REVISION.findall(raw)
        if revisions and (len(revisions) != 1 or int(revisions[0]) != release.version):
            continue
        stem = " ".join(REVISION.sub("", raw).split()).casefold()
        pairs[rid, feed, stem].append((torrent["url"], torrent["name"], release.version or 1))
    return {
        (rid, url, name)
        for (rid, _, _), rows in pairs.items()
        if rid not in numbered
        and len({url for url, _, _ in rows}) > 1
        and len({version for _, _, version in rows}) > 1
        for url, name, _ in rows
    }
