"""Collapse episodes into selectable release variants without hiding ambiguity."""

import hashlib
import json
import re


def version_traits(candidate):
    traits = [candidate.get(k) for k in ("group", "resolution", "source", "subtitle")]
    match = re.search(
        r"(?<![A-Za-z0-9])(HEVC|AVC|x265|x264|H\.265|H\.264)(?![A-Za-z0-9])",
        candidate["name"],
        re.I,
    )
    codec = match[0] if match else None
    terms = list(dict.fromkeys(t for t in [*traits, codec] if t))
    identity = [t.casefold() for t in terms]
    # Preserve published single-episode IDs so saved selections remain valid.
    if candidate.get("episode_end") is not None:
        identity = {"kind": "batch", "terms": identity}
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:20]
    return key, terms, codec


def variants(candidates):
    groups = {}
    for candidate in candidates:
        if not candidate.get("episode_key"):
            continue
        key, terms, codec = version_traits(candidate)
        batch = candidate.get("episode_end") is not None
        entry = groups.setdefault(
            key,
            {
                "id": key,
                "include": terms,
                "group": candidate.get("group") or "字幕组未识别",
                "resolution": candidate["resolution"],
                "source": candidate.get("source"),
                "subtitle": candidate.get("subtitle"),
                "codec": codec,
                "kind": "batch" if batch else "feature" if candidate.get("feature") else "single",
                "episodes": set(),
                "ranges": set(),
                "example": candidate["name"],
            },
        )
        if batch:
            entry["ranges"].add((candidate["episode"], candidate["episode_end"]))
        elif candidate.get("episode") is not None:
            entry["episodes"].add(candidate["episode"])
        entry["example"] = candidate["name"]
    return [
        {
            **v,
            "episodes": sorted(v["episodes"]),
            "ranges": sorted(v["ranges"]),
            "count": {"batch": len(v["ranges"]), "single": len(v["episodes"]), "feature": 1}[
                v["kind"]
            ],
        }
        for v in groups.values()
    ]
