"""Recognize unnumbered pack titles using the verified torrent's video filenames."""

import asyncio
import re
import time
import unicodedata
from pathlib import PurePosixPath

import httpx2

from .batch import SEASON, parse_review_release
from .feature import dedicated_feed, normalized_title

TORRENT = re.compile(
    r"https://(?:mikanani\.me|mikanime\.tv)/Download/\d{8}/([a-fA-F0-9]{40})\.torrent"
)
LIMIT = 4 * 1024 * 1024
VIDEO = {
    ".mkv",
    ".mp4",
    ".avi",
    ".m4v",
    ".mov",
    ".ts",
    ".m2ts",
    ".webm",
    ".wmv",
    ".flv",
    ".mpg",
    ".mpeg",
    ".vob",
    ".iso",
}
EXTRA_DIRS = {"sps", "extras", "bonus", "特典", "映像特典"}
GROUP = re.compile(r"^\s*(?:\[[^\]]+\]|【[^】]+】)\s*")
# Keep the title preceding the actual episode marker; remove release tags afterwards.
MARKER = re.compile(
    r"(?i)(?:\[(\d{1,3})\]|\s-\s*(\d{1,3})(?=\s|$)|(?<!\w)S(\d{1,2})E(\d{1,3})(?!\d))"
)


def decode_paths(data, digest):
    """Decode a bounded v1 metainfo dictionary, checking the raw info hash."""
    from module.downloader.client.qb_downloader import _torrent_infohash

    if not data or len(data) > LIMIT:
        raise ValueError("invalid torrent size")
    pos = nodes = 0

    def read(depth=0):
        nonlocal pos, nodes
        nodes += 1
        if depth > 24 or nodes > 100_000 or pos >= len(data):
            raise ValueError("invalid bencode")
        tag = data[pos : pos + 1]
        if tag.isdigit():
            end = data.index(b":", pos, pos + 10)
            size = data[pos:end]
            if not size.isdigit() or end + 1 + int(size) > len(data):
                raise ValueError("invalid string")
            pos = end + 1 + int(size)
            return data[end + 1 : pos]
        pos += 1
        if tag == b"i":
            end = data.index(b"e", pos, pos + 24)
            num = int(data[pos:end])
            pos = end + 1
            return num
        if tag not in (b"d", b"l"):
            raise ValueError("invalid type")
        result = {} if tag == b"d" else []
        while data[pos : pos + 1] != b"e":
            key = read(depth + 1)
            if tag == b"d":
                if not isinstance(key, bytes) or key in result:
                    raise ValueError("invalid key")
                result[key] = read(depth + 1)
            else:
                result.append(key)
        pos += 1
        return result

    root = read()
    if pos != len(data) or not isinstance(root, dict) or _torrent_infohash(data) != digest:
        raise ValueError("unexpected info hash")
    info = root.get(b"info")
    if not isinstance(info, dict):
        raise ValueError("missing info")
    paths = []
    files = info.get(b"files", [])
    if not isinstance(files, list):
        raise ValueError("invalid file list")
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("invalid file")
        parts = item.get(b"path.utf-8", item.get(b"path"))
        if not isinstance(parts, list) or not parts or not all(isinstance(p, bytes) for p in parts):
            raise ValueError("invalid path")
        if not any(parts[-1].lower().endswith(s.encode()) for s in VIDEO):
            continue
        if type(item.get(b"length")) is not int or item[b"length"] <= 0 or b"symlink path" in item:
            raise ValueError("invalid video file")
        decoded = [p.decode("utf-8") for p in parts]
        if any(p in ("", ".", "..") or "/" in p or "\\" in p for p in decoded):
            raise ValueError("invalid path component")
        if not any(p.casefold() in EXTRA_DIRS for p in decoded[:-1]):
            paths.append("/".join(decoded))
    return paths


async def fetch_paths(url, digest):
    from module.network.request_url import get_shared_client

    client = await get_shared_client()
    data = bytearray()
    async with asyncio.timeout(10), client.stream("GET", url, follow_redirects=False) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes(chunk_size=65536):
            if len(data) + len(chunk) > LIMIT:
                raise ValueError("torrent too large")
            data.extend(chunk)
    return await asyncio.to_thread(decode_paths, bytes(data), digest)


def episode_range(paths, name, rule):
    from module.parser.analyser.tokenizer.result import MediaType, ReleaseKind

    titles = {normalized_title(t) for t in (rule.official_title, rule.title_raw)}
    release = parse_review_release(name)[1]
    if not release:
        return None
    titles.update(
        normalized_title(t) for t in (release.title_en, release.title_jp, release.title_zh)
    )
    # Publishers often provide a clean alternative in a slash-delimited title.
    parts = GROUP.sub("", name).split(" / ")
    titles.update(normalized_title(part) for part in parts[:-1])
    numbers = set()
    common_title = None
    for path in paths:
        stem = unicodedata.normalize("NFKC", PurePosixPath(path).stem)
        clean = GROUP.sub("", stem)
        markers = list(MARKER.finditer(clean))
        if len(markers) != 1:
            return None
        marker = markers[0]
        title = normalized_title(clean[: marker.start()].strip(" -"))
        episode = int(marker[1] or marker[2] or marker[4])
        parsed = parse_review_release(stem)[1]
        if (
            not parsed
            or parsed.release_kind is not ReleaseKind.SINGLE
            or parsed.media_type is not MediaType.EPISODE
            or parsed.episode != episode
            or parsed.season not in (None, rule.season)
            or not title
            or title not in titles
            or common_title not in (None, title)
            or episode in numbers
        ):
            return None
        # Nested directories can carry a season omitted from the filename.
        if any(SEASON.search(folder) for folder in PurePosixPath(path).parts[:-1]):
            return None
        numbers.add(episode)
        common_title = title
    if len(numbers) >= 2 and max(numbers) - min(numbers) + 1 == len(numbers):
        return min(numbers), max(numbers)
    return None


class FileBatches:
    def __init__(self, store):
        self.store = store
        self.retry_at = {}

    async def identify(self, rules, records, sources, features, *, network):
        from module.database.bangumi import _all_title_patterns, _normalize_group_name
        from module.parser.analyser.tokenizer.result import MediaType, ReleaseKind

        cache = await self.store.file_lists()
        proofs = {}
        budget = 4 if network else 0
        seen = set()
        for rid, torrent in records:
            url, name = torrent["url"], torrent["name"]
            key = rid, url, name
            rule = rules.get(rid)
            feed = sources.get(torrent.get("rss_id"))
            match = TORRENT.fullmatch(url)
            if (
                not rule
                or rule.episode_type != "episode"
                or not match
                or not feed
                or key in seen
                or key in features
            ):
                continue
            if feed not in {dedicated_feed(link) for link in (rule.rss_link or "").split(",")}:
                continue
            seen.add(key)
            release = parse_review_release(name)[1]
            if (
                not release
                or release.episode is not None
                or release.season not in (None, rule.season)
                or release.release_kind
                not in (ReleaseKind.SINGLE, ReleaseKind.BATCH, ReleaseKind.COLLECTION)
                or release.media_type not in (MediaType.UNKNOWN, MediaType.EPISODE)
            ):
                continue
            names = {
                normalized_title(t) for t in (*_all_title_patterns(rule), rule.official_title) if t
            }
            if (
                not any(
                    normalized_title(t) in names
                    for t in (release.title_en, release.title_jp, release.title_zh)
                    if t
                )
                or not release.group
                or not rule.group_name
                or _normalize_group_name(rule.group_name) != _normalize_group_name(release.group)
            ):
                continue
            digest = match[1].lower()
            if digest not in cache:
                if not budget or time.monotonic() < self.retry_at.get(digest, 0):
                    continue
                budget -= 1
                try:
                    paths = await fetch_paths(url, digest)
                except httpx2.HTTPError, ValueError, TimeoutError:
                    self.retry_at[digest] = time.monotonic() + 1800
                    continue
                await self.store.save_file_list(digest, paths)
                cache[digest] = paths
                self.retry_at.pop(digest, None)
            span = episode_range(cache[digest], name, rule)
            if span:
                proofs[key] = span
        return proofs
