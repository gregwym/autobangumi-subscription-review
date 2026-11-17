#!/usr/bin/env python3
"""Change only downloader provider IDs in a stopped AB's config; preserve secrets."""

import argparse
import datetime
import json
import os
import tempfile
from pathlib import Path

PROVIDER = "subscription-review-qbittorrent"


def configure(path, instances, apply=False, restore=False):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("config must be a regular, non-symlink file")
    raw = path.read_bytes()
    config = json.loads(raw)
    rows = config["plugins"]["instances"]
    expected, target = (PROVIDER, "qbittorrent") if restore else ("qbittorrent", PROVIDER)
    selected = [r for r in rows if r["id"] in instances and r["point"] == "downloader"]
    if {r["id"] for r in selected} != set(instances):
        raise ValueError("one or more downloader instance IDs were not found")
    if any(r["provider"] not in (expected, target) for r in selected):
        raise ValueError("this plugin supports qBittorrent instances only")
    changed = [r for r in selected if r["provider"] != target]
    for row in changed:
        row["provider"] = target
    if not apply or not changed:
        return {"changed_instances": [r["id"] for r in changed], "applied": False}
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(path.name + ".before-review-" + stamp)
    fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    fd, name = tempfile.mkstemp(prefix=".review-config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, path.stat().st_mode & 0o777)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return {"changed_instances": [r["id"] for r in changed], "applied": True, "backup": str(backup)}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("config", help="Path to config/config.json; stop AB before --apply")
    p.add_argument("--instance", action="append", default=None)
    p.add_argument("--apply", action="store_true", help="Write the change after making a backup")
    p.add_argument(
        "--restore",
        action="store_true",
        help="Restore original qbittorrent provider; stop RSS first",
    )
    args = p.parse_args()
    print(
        json.dumps(
            configure(args.config, args.instance or ["default"], args.apply, args.restore),
            ensure_ascii=False,
        )
    )
