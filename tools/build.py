#!/usr/bin/env python3
"""Build a deterministic installable ZIP with an explicit file allowlist."""

import hashlib
import tomllib
import zipfile
from pathlib import Path


def build(root):
    out = root / "dist"
    out.mkdir(exist_ok=True)
    version = tomllib.loads((root / "plugin.toml").read_text())["plugin"]["version"]
    target = out / f"subscription-review-{version}.zip"
    files = [root / "plugin.toml", root / "README.md", root / "LICENSE"]
    files += sorted((root / "review_gate").glob("*.py"))
    files += [root / "web/index.js"]
    files += [root / "tools/configure.py"]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            info = zipfile.ZipInfo(path.relative_to(root).as_posix(), (2020, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".zip.sha256").write_text(f"{digest}  {target.name}\n")
    print(target)
    return target


if __name__ == "__main__":
    build(Path(__file__).resolve().parents[1])
