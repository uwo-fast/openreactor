import os
from pathlib import Path

import pytest

from openreactor.lock import LOCK_NAME, ControllerLock, LockHeld, state_dir


def test_state_dir_defaults_to_xdg_state_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert state_dir(None) == tmp_path / "openreactor"
    monkeypatch.delenv("XDG_STATE_HOME")
    assert state_dir(None) == Path.home() / ".local" / "state" / "openreactor"
    assert state_dir("/var/lib/openreactor") == Path("/var/lib/openreactor")


def test_lock_creates_the_directory_and_records_its_holder(tmp_path: Path):
    directory = tmp_path / "state"
    with ControllerLock(directory):
        content = (directory / LOCK_NAME).read_text()
        assert content.startswith(f"pid {os.getpid()}:")


def test_a_second_lock_is_refused_naming_the_holder(tmp_path: Path):
    with ControllerLock(tmp_path), pytest.raises(LockHeld) as e:
        ControllerLock(tmp_path).acquire()
    assert e.value.path == tmp_path / LOCK_NAME
    assert e.value.holder.startswith(f"pid {os.getpid()}:")


def test_the_lock_is_free_again_after_release(tmp_path: Path):
    with ControllerLock(tmp_path):
        pass
    with ControllerLock(tmp_path):
        pass
    assert (tmp_path / LOCK_NAME).read_text() == ""


def test_an_unusable_state_directory_is_an_os_error(tmp_path: Path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    with pytest.raises(OSError) as e:
        ControllerLock(blocker / "state").acquire()
    assert e.value.filename == str(blocker / "state")
