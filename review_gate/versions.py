"""Collapse episodes into selectable release variants without hiding ambiguity."""

import hashlib
import json
import re


def variants(candidates):
    groups = {}
    for candidate in candidates:
        terms = [candidate.get(k) for k in ("group", "resolution", "source", "subtitle")]
        codec = re.search(
            r"(?<![A-Za-z0-9])(HEVC|AVC|x265|x264|H\.265|H\.264)(?![A-Za-z0-9])",
            candidate["name"],
            re.I,
        )
        terms.append(codec[0] if codec else None)
        terms = list(dict.fromkeys(t for t in terms if t))
        # Missing resolution is an explicit unknown, not a reason to hide a group.
        if not candidate.get("group"):
            continue
        key = hashlib.sha256(json.dumps([t.casefold() for t in terms]).encode()).hexdigest()[:20]
        entry = groups.setdefault(
            key,
            {
                "id": key,
                "include": terms,
                "group": candidate["group"],
                "resolution": candidate["resolution"],
                "source": candidate.get("source"),
                "subtitle": candidate.get("subtitle"),
                "codec": codec[0] if codec else None,
                "episodes": set(),
                "example": candidate["name"],
            },
        )
        if candidate.get("episode") is not None:
            entry["episodes"].add(candidate["episode"])
        entry["example"] = candidate["name"]
    return [
        {**v, "episodes": sorted(v["episodes"]), "count": len(v["episodes"])}
        for v in groups.values()
    ]
