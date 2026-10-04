import csv
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from fakes import FakeActuator, FakeClock, FakePort

from openreactor.config import DeviceConfig
from openreactor.controller import TICK_S, Controller, Event
from openreactor.ezo import EzoChannel, EzoReader, Outcome, Result, Value
from openreactor.storage import SCHEMA_VERSION, Recorder, StorageError, Store, default_path

T0 = 1_760_000_000.0


class Wall:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "state" / "openreactor.db", wall=Wall())
    yield s
    s.close()


def test_default_path_follows_xdg_state_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert default_path() == tmp_path / "openreactor" / "openreactor.db"
    monkeypatch.setenv("XDG_STATE_HOME", "relative/dir")  # ignored, per the XDG spec
    assert default_path() == Path.home() / ".local" / "state" / "openreactor" / "openreactor.db"


# Schema


def test_a_new_database_gets_the_schema_in_wal_mode(store: Store, tmp_path: Path):
    db = sqlite3.connect(tmp_path / "state" / "openreactor.db")
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"channels", "runs", "readings", "events", "profiles"}


def test_reopening_keeps_the_data(tmp_path: Path):
    path = tmp_path / "openreactor.db"
    s = Store(path)
    run = s.start_run("first", "config")
    s.close()
    s = Store(path)
    assert [r.id for r in s.runs()] == [run]
    s.close()


def test_a_database_of_another_schema_version_is_refused(tmp_path: Path):
    path = tmp_path / "openreactor.db"
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 99")
    db.close()
    with pytest.raises(StorageError, match="schema version 99"):
        Store(path)


# Runs


def test_a_run_lifecycle(store: Store):
    run = store.start_run("brew", "[controller]\n", notes="first try")
    [r] = store.runs()
    assert (r.id, r.name, r.notes, r.status, r.ended) == (run, "brew", "first try", "running", None)

    store.end_run(run, "stopped")
    r = store.run(run)
    assert r is not None and r.status == "stopped" and r.ended is not None

    store.end_run(run, "interrupted")  # an ended run keeps its status
    assert store.run(run).status == "stopped"  # type: ignore[union-attr]


def test_runs_left_running_are_marked_interrupted(store: Store):
    a = store.start_run("a", "c")
    b = store.start_run("b", "c")
    store.end_run(b, "stopped")
    assert store.interrupt_stale_runs() == [a]
    assert [(r.id, r.status) for r in store.runs()] == [(a, "interrupted"), (b, "stopped")]
    assert store.interrupt_stale_runs() == []


def test_status_is_constrained(store: Store):
    run = store.start_run("a", "c")
    with pytest.raises(sqlite3.IntegrityError):
        store.end_run(run, "finished")


# Recording


def recorder(store: Store, run: int) -> Recorder:
    devices = {"ph": ("ph", "ezo-ph"), "do": ("do", "ezo-do")}
    return Recorder(store, run, devices, wall=lambda: T0)


def test_readings_and_events_are_recorded(store: Store):
    run = store.start_run("brew", "c")
    rec = recorder(store, run)
    rec(Result("ph", Outcome.OK, (Value("", 7.02, "pH"),)))
    rec(Result("do", Outcome.OK, (Value("mg_l", 8.1, "mg/L"), Value("saturation", 94.0, "%"))))
    rec(Result("ph", Outcome.FAIL, detail=""))
    rec(Event(T0, "user", "stop-all", device="heater"))

    out = Path(store._db.execute("PRAGMA database_list").fetchone()[2]).parent / "x.zip"  # pyright: ignore[reportPrivateUsage]
    store.export(run, out)
    with zipfile.ZipFile(out) as z:
        readings = list(csv.reader(io.StringIO(z.read("readings.csv").decode())))
        events = list(csv.reader(io.StringIO(z.read("events.csv").decode())))
        meta = json.loads(z.read("run.json"))

    assert readings[0] == ["time", "channel", "value", "unit"]
    assert [row[1:] for row in readings[1:]] == [
        ["ph", "7.02", "pH"],
        ["do.mg_l", "8.1", "mg/L"],
        ["do.saturation", "94.0", "%"],
    ]
    assert readings[1][0] == "2025-10-09T08:53:20+00:00"
    assert events[0] == ["time", "source", "kind", "device", "channel", "details", "result"]
    assert [row[1:] for row in events[1:]] == [
        ["system", "read", "ph", "ph", "", "failed"],
        ["user", "stop-all", "heater", "", "", "ok"],
    ]
    assert meta["name"] == "brew" and meta["status"] == "running" and meta["config"] == "c"


def test_channels_are_upserted_with_their_device_and_unit(store: Store):
    run = store.start_run("brew", "c")
    rec = recorder(store, run)
    rec(Result("do", Outcome.OK, (Value("mg_l", 8.1, "mg/L"),)))
    rows = store._db.execute("SELECT name, device, kind, unit FROM channels").fetchall()  # pyright: ignore[reportPrivateUsage]
    assert rows == [("do.mg_l", "do", "ezo-do", "mg/L")]


def test_export_of_an_unknown_run(store: Store, tmp_path: Path):
    with pytest.raises(StorageError, match="no run 7"):
        store.export(7, tmp_path / "x.zip")


def test_a_failing_write_interrupts_the_run_but_not_control(store: Store):
    """Storage must never stop control: a full disk stops recording and marks
    the run interrupted, while the tick, the reads and stop-all go on."""
    run = store.start_run("brew", "c")
    clock = FakeClock()
    port = FakePort("ph", (Value("", 7.0, "pH"),))
    reader = EzoReader(
        [EzoChannel(DeviceConfig("ph", "ezo-ph", "/dev/i2c-1", 0x63), port)],
        clock=clock,
        sleep=clock.sleep,
    )
    log: list[str] = []
    c = Controller(
        reader,
        [FakeActuator("heater", log)],
        ezo_period_s=2.0,
        auto_read=True,
        clock=clock,
        sleep=clock.sleep,
    )
    rec = recorder(store, run)
    after: list[Result | Event] = []
    c.subscribe(rec)
    c.subscribe(after.append)

    real = store.add_reading
    calls = {"n": 0}

    def full_disk(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise sqlite3.OperationalError("database or disk is full")
        return real(*args, **kwargs)

    store.add_reading = full_disk  # type: ignore[method-assign]

    for _ in range(60):  # six seconds: three read cycles
        c.step()
        clock.now = round(clock.now + TICK_S, 9)
    stop = c.stop_all()
    c.step()

    assert rec.failed == "database or disk is full"
    assert store.run(run).status == "interrupted"  # type: ignore[union-attr]
    # Reads went on after the failure: cycles at 0, 2, 4 and, on the
    # stop-all tick, 6 s, whose result is still pending.
    assert port.sent.count("read") == 4
    assert sum(isinstance(i, Result) for i in after) == 3
    assert stop.done() and log == ["safe heater"]
    assert calls["n"] == 2  # recording stopped after the failure
