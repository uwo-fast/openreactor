"""One controller at a time: a lock file held for as long as a controller owns
the bus.

The lock is machine-wide, not per user, because the bus is: a command run
with sudo and one run by a lab user must refuse each other. It lives in
/run/lock, which every user can write to, and is created readable and
writable by everyone so any user's command can take it, or say who holds it.
"""

from __future__ import annotations

import contextlib
import fcntl
import getpass
import os
import sys
from pathlib import Path

LOCK_PATH = Path("/run/lock/openreactor.lock")


class LockHeld(Exception):
    """Another controller holds the lock."""

    def __init__(self, path: Path, holder: str):
        super().__init__(f"{path} is held by {holder}")
        self.path = path
        self.holder = holder


class ControllerLock:
    """An exclusive, non-blocking ``flock`` on ``path``.

    The file records the holder's user, pid and command line so a refused
    command can say who holds it. The kernel releases the lock when the
    process exits, even if it is killed.
    """

    def __init__(self, path: Path = LOCK_PATH):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
        except PermissionError as e:
            raise PermissionError(
                e.errno, f"cannot open the controller lock: {e.strerror}", str(self.path)
            ) from e
        # The umask may have narrowed the mode; widen it so other users can
        # open the file. Only its creator can, which is enough.
        with contextlib.suppress(PermissionError):
            os.fchmod(fd, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.pread(fd, 4096, 0).decode(errors="replace").strip() or "another process"
            os.close(fd)
            raise LockHeld(self.path, holder) from None
        os.ftruncate(fd, 0)
        holder = f"{getpass.getuser()}, pid {os.getpid()}: {' '.join(sys.argv)}\n"
        os.pwrite(fd, holder.encode(), 0)
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            os.ftruncate(self._fd, 0)
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> ControllerLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
