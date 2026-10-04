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
    value REAL NOT NULL
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
    return "" if t is None else datetime.fromtimestamp(t, UTC).isoformat()


class Store:
    """One SQLite database in WAL mode."""

    def __init__(self, path: Path, wall: Callable[[], float] = time.time):
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the CLI opens it, the controller's thread
        # (the only writer) uses it.
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._wall = wall
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = NORMAL")
        self._db.execute("PRAGMA foreign_keys = ON")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            # One script, so the tables and the version are written together.
            self._db.executescript(
                f"BEGIN;\n{_SCHEMA}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
            )
        elif version != SCHEMA_VERSION:
            self._db.close()
            raise StorageError(
                f"{path} has schema version {version}; this openreactor uses {SCHEMA_VERSION}"
            )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._db.execute("BEGIN")
        try:
            yield
        except BaseException:
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

    def add_reading(
        self, run: int, channel: str, device: str, kind: str, unit: str, at: float, value: float
    ) -> None:
        with self._transaction():
            self._db.execute(
                "INSERT INTO channels (name, device, kind, unit) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET device = excluded.device, "
                "kind = excluded.kind, unit = excluded.unit",
                (channel, device, kind, unit),
            )
            self._db.execute(
                "INSERT INTO readings (run, channel, time, value) VALUES (?, ?, ?, ?)",
                (run, channel, at, value),
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
            "SELECT r.time, r.channel, r.value, c.unit FROM readings r "
            "JOIN channels c ON c.name = r.channel WHERE r.run = ? ORDER BY r.time, r.rowid",
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
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as z:
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
                for v in item.values:
                    channel = f"{item.channel}.{v.field}" if v.field else item.channel
                    self.store.add_reading(self.run, channel, device, kind, v.unit, at, v.value)
        except sqlite3.Error as e:
            # Recording stops; control does not. The run is marked
            # interrupted if the database still accepts that much.
            self.failed = str(e)
            log.error("recording stopped: %s", e)
            try:
                self.store.end_run(self.run, "interrupted")
            except sqlite3.Error:
                log.error("could not mark run %s interrupted", self.run)
