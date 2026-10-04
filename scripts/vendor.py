"""Download the files the vendor manifest lists and record their sha256 there.

    uv run python scripts/vendor.py           # download, then write the hashes
    uv run python scripts/vendor.py --check   # verify the files, no network

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


def check() -> int:
    bad = 0
    for e in entries():
        path = VENDOR / e["path"]
        if not path.exists():
            print(f"missing: {e['path']}", file=sys.stderr)
            bad += 1
        elif sha256(path.read_bytes()) != e["sha256"]:
            print(f"changed: {e['path']} does not match the manifest", file=sys.stderr)
            bad += 1
    return 1 if bad else 0


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
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify only; no network")
    return check() if parser.parse_args().check else update()


if __name__ == "__main__":
    sys.exit(main())
