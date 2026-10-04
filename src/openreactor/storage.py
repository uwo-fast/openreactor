"""Runs, readings and events in SQLite.

The controller's thread is the only writer: a ``Recorder`` subscribes to the
controller and writes what it publishes. Storage must never stop control, so
a failing write (a full disk, say) is logged, the run is marked
``interrupted``, recording stops, and the tick and stop-all carry on.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import sqlite3
import time
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from openreactor.controller import Event
from openreactor.ezo import Outcome, Result

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE channels (
    name TEXT PRIMARY KEY,
    device TEXT NOT NULL,
    kind TEXT NOT NULL,
    unit TEXT NOT NULL
);
CREATE TABLE runs (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    started REAL NOT NULL,
    ended REAL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'stopped', 'interrupted')),
    config TEXT NOT NULL,
    profile TEXT
);
CREATE TABLE readings (
    run INTEGER NOT NULL REFERENCES runs (id),
    channel TEXT NOT NULL REFERENCES channels (name),
    time REAL NOT NULL,
    value REAL NOT NULL,
    -- The unit as read: a channel name can be reused later with another unit.
    unit TEXT NOT NULL
);
CREATE INDEX readings_by_run ON readings (run, time);
CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    run INTEGER REFERENCES runs (id),
    time REAL NOT NULL,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    device TEXT,
    channel TEXT,
    details TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL
);
CREATE INDEX events_by_run ON events (run, time);
CREATE TABLE profiles (
    name TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    created REAL NOT NULL
);
"""


def default_path() -> Path:
    """``$XDG_STATE_HOME/openreactor/openreactor.db``, else under
    ``~/.local/state``. An installed service sets ``storage.database``."""
    base = os.environ.get("XDG_STATE_HOME", "")
    if not base.startswith("/"):  # the XDG spec says to ignore a relative one
        base = str(Path.home() / ".local" / "state")
    return Path(base) / "openreactor" / "openreactor.db"


class StorageError(Exception):
    """The database cannot be used: the wrong schema version, or unreadable."""


@dataclass(frozen=True)
class Run:
    id: int
    name: str
    notes: str
    started: float
    ended: float | None
    status: str


def _iso(t: float | None) -> str:
    if t is None:
        return ""
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="milliseconds")


# How long a write from the controller's tick may wait for a database lock
# (someone browsing the file, say) before it counts as a failed write. The
# tick must not stall; 50 ms is half a tick.
WRITE_WAIT_S = 0.05

# Opening and starting or ending a run happen outside the tick, and may wait.
SETUP_WAIT_S = 5.0


class Store:
    """One SQLite database in WAL mode. ``read_only`` opens an existing
    database for listing and export, without creating or changing it."""

    def __init__(
        self, path: Path, wall: Callable[[], float] = time.time, *, read_only: bool = False
    ):
        self.path = path
        self._wall = wall
        if read_only:
            if not path.exists():
                raise StorageError(f"{path} does not exist: no runs have been recorded")
            self._db = self._open_read_only(path)
            self._check_version(self._db.execute("PRAGMA user_version").fetchone()[0])
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the CLI opens it, the controller's thread
        # (the only writer) uses it.
        self._db = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None, timeout=SETUP_WAIT_S
        )
        try:
            self._db.execute("PRAGMA synchronous = NORMAL")
            self._db.execute("PRAGMA foreign_keys = ON")
            # Take the write lock before reading the version, so two first
            # opens cannot both create the schema.
            self._db.execute("BEGIN IMMEDIATE")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        self._db.execute(statement)
                self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._db.execute("COMMIT")
            self._check_version(version or SCHEMA_VERSION)
            self._use_wal()
        except BaseException:
            self._db.close()
            raise

    def _use_wal(self) -> None:
        # Switching to WAL needs a moment of exclusive access, which another
        # process opening the file at the same time can hold; the busy
        # handler does not cover it, so retry briefly. Once a database is in
        # WAL mode it stays so, and this is a no-op.
        deadline = time.monotonic() + SETUP_WAIT_S
        while True:
            try:
                self._db.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or time.monotonic() > deadline:
                    raise
                time.sleep(0.01)

    @staticmethod
    def _open_read_only(path: Path) -> sqlite3.Connection:
        uri = f"file:{path}?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=SETUP_WAIT_S, isolation_level=None)
        try:
            db.execute("PRAGMA user_version").fetchone()
            return db
        except sqlite3.OperationalError as e:
            db.close()
            if "readonly" not in str(e):
                raise
        # A WAL database can only be read through its -shm file, which this
        # user cannot create here. The file is absent only when no process
        # has the database open, so nothing is writing it and it can be read
        # as immutable.
        uri = f"file:{path}?mode=ro&immutable=1"
        return sqlite3.connect(uri, uri=True, isolation_level=None)

    def _check_version(self, version: int) -> None:
        if version != SCHEMA_VERSION:
            self._db.close()
            raise StorageError(
                f"{self.path} has schema version {version}; this openreactor uses {SCHEMA_VERSION}"
            )

    def _wait(self, seconds: float) -> None:
        self._db.execute(f"PRAGMA busy_timeout = {int(seconds * 1000)}")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._db.execute("BEGIN")
        try:
            yield
        except BaseException:
            # SQLite has already rolled back after some errors (a full
            # disk); rolling back again would replace the real error.
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def close(self) -> None:
        self._db.close()

    # Runs

    def interrupt_stale_runs(self) -> list[int]:
        """Runs still marked running belong to a process that did not end
        them (a crash, a power cut); they are never resumed."""
        rows = self._db.execute("SELECT id FROM runs WHERE status = 'running'").fetchall()
        ids = [r[0] for r in rows]
        if ids:
            self._db.execute(
                "UPDATE runs SET status = 'interrupted', ended = ? WHERE status = 'running'",
                (self._wall(),),
            )
        return ids

    def start_run(self, name: str, config: str, notes: str = "", profile: str | None = None) -> int:
        cursor = self._db.execute(
            "INSERT INTO runs (name, notes, started, status, config, profile) "
            "VALUES (?, ?, ?, 'running', ?, ?)",
            (name, notes, self._wall(), config, profile),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def end_run(self, run: int, status: str) -> None:
        self._db.execute(
            "UPDATE runs SET status = ?, ended = ? WHERE id = ? AND status = 'running'",
            (status, self._wall(), run),
        )

    def runs(self) -> list[Run]:
        rows = self._db.execute(
            "SELECT id, name, notes, started, ended, status FROM runs ORDER BY id"
        ).fetchall()
        return [Run(*row) for row in rows]

    def run(self, run: int) -> Run | None:
        row = self._db.execute(
            "SELECT id, name, notes, started, ended, status FROM runs WHERE id = ?", (run,)
        ).fetchone()
        return Run(*row) if row else None

    # Readings and events

    def recording(self) -> None:
        """From here on, writes come from the controller's tick: a write
        that cannot get the database lock quickly fails instead of waiting."""
        self._wait(WRITE_WAIT_S)

    def setup(self) -> None:
        """Back to waiting patiently, for starting or ending a run."""
        self._wait(SETUP_WAIT_S)

    def add_readings(
        self, run: int, device: str, kind: str, at: float, values: list[tuple[str, str, float]]
    ) -> None:
        """All of one reading's ``(channel, unit, value)`` outputs, or none."""
        with self._transaction():
            for channel, unit, value in values:
                self._db.execute(
                    "INSERT INTO channels (name, device, kind, unit) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (name) DO UPDATE SET device = excluded.device, "
                    "kind = excluded.kind, unit = excluded.unit",
                    (channel, device, kind, unit),
                )
                self._db.execute(
                    "INSERT INTO readings (run, channel, time, value, unit) VALUES (?, ?, ?, ?, ?)",
                    (run, channel, at, value, unit),
                )

    def add_event(self, run: int | None, event: Event) -> None:
        self._db.execute(
            "INSERT INTO events (run, time, source, kind, device, channel, details, result) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run,
                event.time,
                event.source,
                event.kind,
                event.device,
                event.channel,
                event.details,
                event.result,
            ),
        )

    # Export

    def export(self, run: int, destination: Path) -> None:
        """Write ``readings.csv``, ``events.csv`` and ``run.json`` for one
        run into a zip file."""
        info = self._db.execute(
            "SELECT id, name, notes, started, ended, status, config, profile "
            "FROM runs WHERE id = ?",
            (run,),
        ).fetchone()
        if info is None:
            raise StorageError(f"there is no run {run}")
        id_, name, notes, started, ended, status, config, profile = info

        readings = io.StringIO()
        writer = csv.writer(readings, lineterminator="\n")
        writer.writerow(["time", "channel", "value", "unit"])
        for at, channel, value, unit in self._db.execute(
            "SELECT time, channel, value, unit FROM readings WHERE run = ? ORDER BY time, rowid",
            (run,),
        ):
            writer.writerow([_iso(at), channel, repr(value), unit])

        events = io.StringIO()
        writer = csv.writer(events, lineterminator="\n")
        writer.writerow(["time", "source", "kind", "device", "channel", "details", "result"])
        for row in self._db.execute(
            "SELECT time, source, kind, device, channel, details, result FROM events "
            "WHERE run = ? ORDER BY time, id",
            (run,),
        ):
            writer.writerow([_iso(row[0]), *("" if v is None else v for v in row[1:])])

        meta = {
            "id": id_,
            "name": name,
            "notes": notes,
            "started": _iso(started),
            "ended": _iso(ended) or None,
            "status": status,
            "config": config,
            "profile": profile,
        }
        # "x": never overwrite an earlier export.
        with zipfile.ZipFile(destination, "x", zipfile.ZIP_DEFLATED) as z:
            z.writestr("readings.csv", readings.getvalue())
            z.writestr("events.csv", events.getvalue())
            z.writestr("run.json", json.dumps(meta, indent=2) + "\n")


class Recorder:
    """Writes a run's readings and events as the controller publishes them.
    Subscribe it to the controller, which calls it on the controller's
    thread, so it is the database's only writer while the run lasts."""

    def __init__(
        self,
        store: Store,
        run: int,
        devices: dict[str, tuple[str, str]],
        wall: Callable[[], float] = time.time,
    ):
        # devices: channel or device name -> (device name, kind)
        self.store = store
        self.run = run
        self._devices = devices
        self._wall = wall
        self.failed: str | None = None

    def __call__(self, item: Result | Event) -> None:
        if self.failed is not None:
            return
        try:
            if isinstance(item, Event):
                self.store.add_event(self.run, item)
            elif item.outcome is not Outcome.OK:
                # A failed read is part of the run's record too.
                device, _ = self._devices.get(item.channel, (item.channel, ""))
                event = Event(
                    self._wall(),
                    "system",
                    "read",
                    device=device,
                    channel=item.channel,
                    details=item.detail,
                    result=item.outcome.value,
                )
                self.store.add_event(self.run, event)
            else:
                device, kind = self._devices.get(item.channel, (item.channel, ""))
                at = self._wall()
                values: list[tuple[str, str, float]] = []
                for v in item.values:
                    channel = f"{item.channel}.{v.field}" if v.field else item.channel
                    if math.isfinite(v.value):
                        values.append((channel, v.unit, v.value))
                    else:
                        # Not a number SQLite can store; keep the fact.
                        bad = Event(
                            at,
                            "system",
                            "read",
                            device=device,
                            channel=channel,
                            details=f"non-finite value {v.value}",
                            result="error",
                        )
                        self.store.add_event(self.run, bad)
                if values:
                    self.store.add_readings(self.run, device, kind, at, values)
        except sqlite3.Error as e:
            # Recording stops; control does not. The run is marked
            # interrupted if the database still accepts that much.
            self.failed = str(e)
            log.error("recording stopped: %s", e)
            try:
                self.store.end_run(self.run, "interrupted")
            except sqlite3.Error:
                log.error("could not mark run %s interrupted", self.run)
