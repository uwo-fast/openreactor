import subprocess
import sys
from pathlib import Path

import pytest

from openreactor.cli import main

EXAMPLE = Path(__file__).parents[1] / "examples" / "openreactor.toml"


def test_check_config_passes_on_the_example(capsys: pytest.CaptureFixture[str]):
    assert main(["check-config", str(EXAMPLE)]) == 0
    assert "OK, 5 device(s), 5 channel(s)" in capsys.readouterr().out


def test_check_config_lists_problems(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    bad = tmp_path / "bad.toml"
    bad.write_text("[controller]\nwatchdog_timeout_ms = 70000\nsurprise = 1\n")
    assert main(["check-config", str(bad)]) == 1
    err = capsys.readouterr().err
    assert "error: controller.surprise: unknown key" in err
    assert "error: controller.watchdog_timeout_ms:" in err
    assert "2 problem(s)" in err


def test_check_config_reports_a_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    assert main(["check-config", str(tmp_path / "missing.toml")]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_import_opens_no_device_database_or_socket():
    # Audit hooks see every open(), sqlite3.connect and socket creation, so this
    # catches side effects at import even when nothing is listening.
    code = """
import os, sys
seen = []
def hook(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = os.fsdecode(args[0])
        if path.startswith("/dev/") or path.endswith(".db"):
            seen.append((event, path))
    elif event in ("sqlite3.connect", "socket.__new__", "socket.connect", "socket.bind"):
        seen.append((event, args))
sys.addaudithook(hook)
import openreactor, openreactor.config, openreactor.cli
print(seen)
"""
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "[]"
