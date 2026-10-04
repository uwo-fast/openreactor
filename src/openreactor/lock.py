"""One controller at a time: a lock file in the state directory, held for as
long as a controller owns the bus."""

from __future__ import annotations

import fcntl
import os
import sys
from pathlib import Path

LOCK_NAME = "openreactor.lock"


def state_dir(configured: str | None) -> Path:
    """The configured state directory, or the per-user default
    (``$XDG_STATE_HOME/openreactor``, else ``~/.local/state/openreactor``)."""
    if configured is not None:
        return Path(configured)
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "openreactor"


class LockHeld(Exception):
    """Another controller holds the lock."""

    def __init__(self, path: Path, holder: str):
        super().__init__(f"{path} is held by {holder}")
        self.path = path
        self.holder = holder


class ControllerLock:
    """An exclusive, non-blocking ``flock`` on ``<state_dir>/openreactor.lock``.

    The file records the holder's pid and command line so a refused command
    can say who holds it. The lock is released when the process exits, even
    if it is killed.
    """

    def __init__(self, directory: Path):
        self.path = directory / LOCK_NAME
        self._fd: int | None = None

    def acquire(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise OSError(
                e.errno, f"cannot create the state directory: {e.strerror}", str(self.path.parent)
            ) from e
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.pread(fd, 4096, 0).decode(errors="replace").strip() or "another process"
            os.close(fd)
            raise LockHeld(self.path, holder) from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"pid {os.getpid()}: {' '.join(sys.argv)}\n".encode(), 0)
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
