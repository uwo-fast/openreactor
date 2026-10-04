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


def test_an_existing_lock_file_is_opened_without_o_creat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # fs.protected_regular refuses O_CREAT on another user's file in a
    # sticky directory like /run/lock, even for root.
    path = tmp_path / "openreactor.lock"
    path.write_text("")
    flags: list[int] = []
    real_open = os.open

    def recording_open(p, f, *mode):
        flags.append(f)
        return real_open(p, f, *mode)

    monkeypatch.setattr(os, "open", recording_open)
    with ControllerLock(path):
        pass
    assert flags and not flags[0] & os.O_CREAT


def test_a_lock_file_created_by_another_process_meanwhile_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "openreactor.lock"
    real_open = os.open
    calls = {"n": 0}

    def racing_open(p, f, *mode):
        calls["n"] += 1
        if calls["n"] == 2:  # between our two attempts, someone creates it
            path.write_text("")
            raise FileExistsError(p)
        return real_open(p, f, *mode)

    monkeypatch.setattr(os, "open", racing_open)
    with ControllerLock(path):
        pass
    assert calls["n"] == 3
