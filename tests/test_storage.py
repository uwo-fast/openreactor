import csv
import io
import json
import sqlite3
import time
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
    assert readings[1][0] == "2025-10-09T08:53:20.000+00:00"
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


def fill_up(store: Store) -> None:
    """Make the database full for real: no page may be added."""
    db = store._db  # pyright: ignore[reportPrivateUsage]
    pages = db.execute("PRAGMA page_count").fetchone()[0]
    db.execute(f"PRAGMA max_page_count = {pages}")


def test_a_failing_write_interrupts_the_run_but_not_control(store: Store):
    """Storage must never stop control: a full database stops recording and
    the run is marked interrupted, while the tick, the reads and stop-all go
    on."""
    run = store.start_run("brew", "c")
    clock = FakeClock()
    port = FakePort("do", (Value("mg_l", 8.1, "mg/L"), Value("saturation", 94.0, "%")))
    reader = EzoReader(
        [EzoChannel(DeviceConfig("do", "ezo-do", "/dev/i2c-1", 0x61), port)],
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
    store.recording()

    def step(seconds: float) -> None:
        for _ in range(round(seconds / TICK_S)):
            c.step()
            clock.now = round(clock.now + TICK_S, 9)

    step(2.0)  # one reading recorded
    fill_up(store)
    for _ in range(200):  # until the database is truly full
        if rec.failed is not None:
            break
        step(2.0)
    stop = c.stop_all()
    step(4.0)

    assert rec.failed == "database or disk is full"
    assert store.run(run).status == "interrupted"  # type: ignore[union-attr]
    # Reads and stop-all went on after the failure.
    assert sum(isinstance(i, Result) for i in after) > port.sent.count("read") - 2
    assert stop.done() and log == ["safe heater"]
    # Every recorded reading has both its outputs: none was stored in part.
    db = store._db  # pyright: ignore[reportPrivateUsage]
    counts = dict(db.execute("SELECT channel, count(*) FROM readings GROUP BY channel"))
    assert counts["do.mg_l"] == counts["do.saturation"] >= 1


def test_a_locked_database_fails_the_write_quickly(store: Store, tmp_path: Path):
    """Someone holding the database's write lock (a browser mid-edit) must not
    stall the tick: the write fails within the short wait, as a failed write."""
    run = store.start_run("brew", "c")
    rec = recorder(store, run)
    store.recording()
    other = sqlite3.connect(tmp_path / "state" / "openreactor.db", isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    rec(Result("ph", Outcome.OK, (Value("", 7.0, "pH"),)))
    elapsed = time.monotonic() - started
    other.execute("ROLLBACK")

    assert rec.failed is not None and "locked" in rec.failed
    assert elapsed < 0.5


def test_a_reused_channel_name_keeps_each_runs_unit(store: Store, tmp_path: Path):
    first = store.start_run("a", "c")
    Recorder(store, first, {"probe": ("probe", "ezo-ph")})(
        Result("probe", Outcome.OK, (Value("", 7.0, "pH"),))
    )
    second = store.start_run("b", "c")
    Recorder(store, second, {"probe": ("probe", "ezo-orp")})(
        Result("probe", Outcome.OK, (Value("", 225.0, "mV"),))
    )
    store.export(first, tmp_path / "first.zip")
    with zipfile.ZipFile(tmp_path / "first.zip") as z:
        rows = list(csv.reader(io.StringIO(z.read("readings.csv").decode())))
    assert rows[1][1:] == ["probe", "7.0", "pH"]


def test_a_non_finite_value_is_an_event_not_the_end_of_recording(store: Store):
    run = store.start_run("a", "c")
    rec = recorder(store, run)
    rec(
        Result(
            "do", Outcome.OK, (Value("mg_l", float("nan"), "mg/L"), Value("saturation", 94.0, "%"))
        )
    )
    rec(Result("ph", Outcome.OK, (Value("", 7.0, "pH"),)))
    assert rec.failed is None
    db = store._db  # pyright: ignore[reportPrivateUsage]
    assert db.execute("SELECT channel FROM readings ORDER BY rowid").fetchall() == [
        ("do.saturation",),
        ("ph",),
    ]
    assert db.execute("SELECT channel, details FROM events").fetchall() == [
        ("do.mg_l", "non-finite value nan")
    ]


def test_two_first_opens_do_not_race(tmp_path: Path):
    import threading

    for trial in range(30):
        path = tmp_path / f"db{trial}.db"
        errors: list[BaseException] = []

        def open_it(path: Path = path, errors: list[BaseException] = errors) -> None:
            try:
                Store(path).close()
            except BaseException as e:
                errors.append(e)

        threads = [threading.Thread(target=open_it) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], (trial, errors)


def test_read_only_open_lists_without_creating(tmp_path: Path):
    path = tmp_path / "openreactor.db"
    with pytest.raises(StorageError, match="no runs have been recorded"):
        Store(path, read_only=True)
    assert not path.exists()
    s = Store(path)
    s.start_run("a", "c")
    s.close()
    path.chmod(0o444)
    tmp_path.chmod(0o555)
    try:
        ro = Store(path, read_only=True)
        assert [r.name for r in ro.runs()] == ["a"]
        ro.close()
    finally:
        tmp_path.chmod(0o755)
        path.chmod(0o644)


def test_export_never_overwrites(store: Store, tmp_path: Path):
    run = store.start_run("a", "c")
    out = tmp_path / "x.zip"
    store.export(run, out)
    with pytest.raises(FileExistsError):
        store.export(run, out)
