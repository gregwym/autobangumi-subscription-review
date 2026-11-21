"""Compatibility for numbered releases and bounded TV packs on AB 4.0.0."""

import re
import unicodedata
from dataclasses import replace

SEASON = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:s|season\s*)0*(\d{1,2})(?!\d)|第\s*([零〇一二两兩三四五六七八九十百\d]+)\s*[季期]"
)
CROSS_SEASON = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:s|season\s*)\d{1,2}\s*[-~～–—]\s*"
    r"(?:s\d{1,2}\b|\d{1,2}\b(?!\s*[-~～–—]\s*\d))"
)
EXTRAS = re.compile(r"(?i)\b(?:NCOP|NCED|OP|ED|OVA|OAD|PV|SP|MOVIE)\d*\b|特典|剧场版|劇場版")
EPISODE_RANGE = re.compile(r"(?<![A-Za-z0-9])\d{1,4}\s*[-~–—]\s*\d{1,4}(?!\d)")
LABELED_RANGE = re.compile(
    r"[\[【]\s*(\d{1,4})\s*[-~～–—－]\s*(\d{1,4})(v[1-9]\d{0,2})?\s*[话話集]\s*[\]】]",
    re.I,
)
SPACED_REVISION = re.compile(
    r"(?<=\s)([-–—]\s*\d{1,3}(?:\.5)?)\s+(v[1-9]\d{0,2})(?=\s*(?:[\[【]|$))",
    re.I,
)


def parser_name(name):
    name = LABELED_RANGE.sub(lambda m: f"[{m[1]}-{m[2]}{m[3] or ''}]", name)
    return SPACED_REVISION.sub(r"\1\2", name)


def parse_review_release(name):
    """Rewrite only literal, numbered release syntax for AB's parser."""
    from module.parser.analyser.selector import parse_configured_release_title
    from module.parser.analyser.tokenizer.result import ReleaseKind

    normalized = parser_name(name)
    release = parse_configured_release_title(normalized)
    if (
        normalized != name
        and LABELED_RANGE.search(name)
        and (
            release is None
            or release.release_kind not in {ReleaseKind.RANGE, ReleaseKind.COLLECTION}
        )
    ):
        return normalized, None
    return normalized, release


def global_excluded(name):
    from module.conf import settings

    pattern = "|".join(settings.rss_parser.filter)
    return bool(pattern and re.search(pattern, name))


def bounded_batch(name, release):
    from module.parser.analyser.tokenizer.classic import _chinese_number
    from module.parser.analyser.tokenizer.result import MediaType, ReleaseKind

    if release is None or release.release_kind not in {ReleaseKind.RANGE, ReleaseKind.COLLECTION}:
        return None
    if release.release_kind is ReleaseKind.COLLECTION:
        # Classic calls "[01–06 合集]" a collection. Only accept it when the
        # tokenizer independently identifies a numbered TV range.
        from module.parser.analyser.tokenizer import parser

        release = parser.parse_release_title(parser_name(name))
        if release is None:
            return None
    normalized = unicodedata.normalize("NFKC", name)
    seasons = {
        int(arabic) if arabic else _chinese_number(chinese.replace("兩", "两"))
        for arabic, chinese in SEASON.findall(normalized)
    }
    if release.season is not None:
        seasons.add(release.season)
    if (
        release.release_kind is not ReleaseKind.RANGE
        or release.media_type is not MediaType.EPISODE
        or not release.primary_title
        or 0 in seasons
        or type(release.episode) is not int
        or type(release.episode_end) is not int
        or not 0 <= release.episode < release.episode_end <= 100000
        or len(seasons) > 1
        or CROSS_SEASON.search(normalized)
        or EXTRAS.search(normalized)
        or len(EPISODE_RANGE.findall(normalized)) > 1
    ):
        return None
    return replace(release, raw=name, season=next(iter(seasons), None))


def parsed_batch(name):
    _, release = parse_review_release(name)
    return bounded_batch(name, release)


def batch_matches(release, rules):
    from module.database.bangumi import _all_title_patterns

    return [
        (rule, length)
        for rule in rules
        if rule.episode_type == "episode"
        and (release.season is None or release.season == rule.season)
        and (
            length := max(
                (
                    len(pattern)
                    for pattern in _all_title_patterns(rule)
                    if pattern in release.raw or pattern == release.primary_title
                ),
                default=0,
            )
        )
    ]


async def project_batch(analyser, rss, torrent, release):
    from module.parser import TitleParser

    official_title, title_raw = TitleParser._resolve_titles(release)
    rule = TitleParser._release_to_bangumi(release, official_title, title_raw)
    rule.eps_collect = True  # The review queue owns this resource.
    rule.rss_link = rss.url
    rule.downloader_id = rss.downloader_id
    await analyser.official_title_parser(rule, rss, torrent)
    return rule


async def discover_batches(analyser, rss, engine, torrents, rules, match_rule):
    new = []
    for torrent, release in torrents:
        if global_excluded(torrent.name):
            continue
        if batch_matches(release, rules + new):
            # Reuse an existing rule. Do not create another if the season or
            # title is ambiguous, or if the user's rule excludes this resource.
            rule = match_rule(torrent.name, rules, batch=release)
            if rule and rss.url not in rule.rss_link.split(","):
                if rule.filter and engine._get_filter_pattern(rule.filter).search(torrent.name):
                    continue
                rule.rss_link = ",".join(filter(None, (rule.rss_link, rss.url)))
                engine.db.add(rule)
            continue
        rule = await project_batch(analyser, rss, torrent, release)
        if rule.filter and engine._get_filter_pattern(rule.filter).search(torrent.name):
            continue
        new.append(rule)
    if new:
        await engine.db.bangumi.add_all(new)
    await engine.db.commit()
    return new
