#!/usr/bin/env python3
"""Offline fallback: change provider IDs in a stopped AB config."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from review_gate.configuration import PROVIDER, configure  # noqa: E402,F401

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
