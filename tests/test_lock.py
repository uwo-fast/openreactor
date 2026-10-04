import getpass
import os
import stat
from pathlib import Path

import pytest

from openreactor.lock import LOCK_PATH, ControllerLock, LockHeld


def test_the_lock_is_machine_wide():
    # Not under a home or state directory: a command run with sudo and one
    # run by a lab user must find the same lock.
    assert Path("/run/lock/openreactor.lock") == LOCK_PATH


def test_lock_records_its_holder(tmp_path: Path):
    path = tmp_path / "openreactor.lock"
    with ControllerLock(path):
        assert path.read_text().startswith(f"{getpass.getuser()}, pid {os.getpid()}:")


def test_the_lock_file_is_open_to_every_user(tmp_path: Path):
    path = tmp_path / "openreactor.lock"
    old = os.umask(0o022)
    try:
        with ControllerLock(path):
            pass
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o666


def test_a_second_lock_is_refused_naming_the_holder(tmp_path: Path):
    path = tmp_path / "openreactor.lock"
    with ControllerLock(path), pytest.raises(LockHeld) as e:
        ControllerLock(path).acquire()
    assert e.value.path == path
    assert e.value.holder.startswith(f"{getpass.getuser()}, pid {os.getpid()}:")


def test_the_lock_is_free_again_after_release(tmp_path: Path):
    path = tmp_path / "openreactor.lock"
    with ControllerLock(path):
        pass
    with ControllerLock(path):
        pass
    assert path.read_text() == ""


def test_a_lock_that_cannot_be_opened_names_the_file(tmp_path: Path):
    path = tmp_path / "missing" / "openreactor.lock"
    with pytest.raises(OSError) as e:
        ControllerLock(path).acquire()
    assert e.value.filename == str(path)
