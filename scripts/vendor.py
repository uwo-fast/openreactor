"""Download the files the vendor manifest lists and record their sha256 there.

    uv run python scripts/vendor.py   # download, then write the hashes

A download whose hash differs from a recorded one is written anyway and the
new hash recorded, so the change shows in the diff for review.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "src" / "openreactor" / "static" / "vendor"
MANIFEST = VENDOR / "vendor.toml"


def entries() -> list[dict[str, str]]:
    return tomllib.loads(MANIFEST.read_text())["file"]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def update() -> int:
    files = entries()
    text = MANIFEST.read_text()
    if len(re.findall(r'^sha256 = ".*"$', text, flags=re.M)) != len(files):
        print("every [[file]] in the manifest needs one sha256 line", file=sys.stderr)
        return 1
    # Download everything first: a failure partway leaves nothing changed.
    downloads: list[bytes] = []
    for e in files:
        url = e["url"].format(version=e["version"])
        with urllib.request.urlopen(url, timeout=60) as response:
            downloads.append(response.read())
    hashes: list[str] = []
    for e, data in zip(files, downloads, strict=True):
        path = VENDOR / e["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        digest = sha256(data)
        note = "" if digest == e["sha256"] else f" (was {e['sha256'] or 'unset'})"
        print(f"{e['path']}: {e['package']} {e['version']}, {len(data)} bytes, {digest}{note}")
        hashes.append(digest)
    # Replace each sha256 line in order, keeping the file's comments.
    lines = iter(hashes)
    text = re.sub(r'^sha256 = ".*"$', lambda _: f'sha256 = "{next(lines)}"', text, flags=re.M)
    MANIFEST.write_text(text)
    return 0


def main() -> int:
    argparse.ArgumentParser(description=(__doc__ or "").partition("\n")[0]).parse_args()
    return update()


if __name__ == "__main__":
    sys.exit(main())
