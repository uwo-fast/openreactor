"""The vendored browser files are exactly what their manifest records."""

import hashlib
import tomllib
from pathlib import Path

VENDOR = Path(__file__).parents[1] / "src" / "openreactor" / "static" / "vendor"


def test_every_vendored_file_matches_its_recorded_hash():
    files = tomllib.loads((VENDOR / "vendor.toml").read_text())["file"]
    assert files
    for f in files:
        path = VENDOR / f["path"]
        assert path.exists(), f["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == f["sha256"], (
            f"{f['path']} differs from vendor.toml: run `just vendor`, never edit it by hand"
        )
