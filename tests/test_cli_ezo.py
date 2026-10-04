import csv
import getpass
import io
import json
import os
import signal
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest
from fakes import FakeActuator, FakeClock, FakeI2cBus, FakePort, FakeRlht

from openreactor import cli
from openreactor.config import DeviceConfig
from openreactor.ezo import EzoDeviceError, Outcome, Value
from openreactor.lock import ControllerLock

CONFIG = """
[[device]]
name = "heater"
kind = "rlht"
bus = "/dev/i2c-1"
address = 0x0A
channels.jacket = { output = 1, tc = 1 }

[[device]]
name = "vessel_temp"
kind = "ezo-rtd"
bus = "/dev/i2c-1"
address = 0x66

[[device]]
name = "ph"
kind = "ezo-ph"
bus = "/dev/i2c-1"
address = 0x63
temp_comp = "vessel_temp"

[[device]]
name = "do"
kind = "ezo-do"
bus = "/dev/i2c-1"
address = 0x61
"""


@pytest.fixture(autouse=True)
def lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # The machine-wide lock lives in /run/lock; tests use their own file.
    path = tmp_path / "openreactor.lock"
    monkeypatch.setattr(cli, "lock_path", path)
    return path


@pytest.fixture(autouse=True)
def heater(monkeypatch: pytest.MonkeyPatch) -> FakeRlht:
    """The config's RLHT at 0x0A, on a fake bus: no test reaches /dev/i2c."""
    rlht = FakeRlht()
    monkeypatch.setattr(cli, "open_bus", lambda path: FakeI2cBus({0x0A: rlht}))
    return rlht


@pytest.fixture
def config(tmp_path: Path) -> str:
    path = tmp_path / "openreactor.toml"
    path.write_text(CONFIG)
    return str(path)


@pytest.fixture
def ports(monkeypatch: pytest.MonkeyPatch) -> dict[str, FakePort]:
    made = {
        "vessel_temp": FakePort("rtd", (Value("", 24.5, "°C"),)),
        "ph": FakePort("ph", (Value("", 6.98, "pH"),)),
        "do": FakePort(
            "do",
            (Value("mg_l", 8.12, "mg/L"), Value("saturation", 97.4, "%")),
            config=0b11,
        ),
    }

    def open_port(d: DeviceConfig) -> FakePort:
        return made[d.name]

    clock = FakeClock()
    monkeypatch.setattr(cli, "open_port", open_port)
    monkeypatch.setattr(cli, "clock", clock)
    monkeypatch.setattr(cli, "sleep", clock.sleep)
    return made


def test_read_once_prints_every_channel(config: str, ports, capsys: pytest.CaptureFixture[str]):
    assert cli.main(["read", "--once", "-c", config]) == 0
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        "vessel_temp                  24.500 °C",
        "ph                            6.980 pH  (compensated at 24.50 °C)",
        "do.mg_l                       8.120 mg/L",
        "do.saturation                97.400 %",
    ]
    assert err == "note: not reading heater: slices are not read yet\n"
    assert all(p.closed for p in ports.values())


def test_read_once_fails_when_a_channel_fails(config: str, ports, capsys):
    ports["ph"].replies.append(Outcome.FAIL)
    assert cli.main(["read", "--once", "-c", config]) == 1
    assert "ph                       failed" in capsys.readouterr().out


def test_read_once_fails_on_a_wrong_circuit(config: str, ports, capsys):
    ports["do"].reports = "ec"
    assert cli.main(["read", "--once", "-c", config]) == 1
    out = capsys.readouterr().out
    assert "do                       error: config says ezo-do, the circuit reports 'ec'" in out
    assert "do.mg_l" not in out


def test_read_once_reports_a_circuit_that_cannot_be_opened(
    config: str, ports, monkeypatch: pytest.MonkeyPatch, capsys
):
    def open_port(d: DeviceConfig) -> FakePort:
        if d.name == "do":
            raise EzoDeviceError("cannot open /dev/i2c-1 at 0x61: transport failure")
        return ports[d.name]

    monkeypatch.setattr(cli, "open_port", open_port)
    assert cli.main(["read", "--once", "-c", config]) == 1
    out = capsys.readouterr().out
    assert "do                       error: cannot open /dev/i2c-1 at 0x61" in out
    assert "ph                            6.980 pH" in out


def test_cal_status(config: str, ports, capsys):
    ports["ph"].calibration = "two point"
    assert cli.main(["ezo", "cal", "-c", config, "ph", "status"]) == 0
    assert capsys.readouterr().out == "ph: two point\n"
    assert ports["ph"].sent == ["info", "config", "cal?"]


def test_cal_point_then_shows_status(config: str, ports, capsys):
    assert cli.main(["ezo", "cal", "-c", config, "ph", "mid", "7.00"]) == 0
    assert capsys.readouterr().out == "ph: calibrated at mid\n"
    assert ports["ph"].sent == ["info", "config", "cal mid 7.0", "cal?"]


def test_cal_clear_needs_yes(config: str, ports, capsys):
    assert cli.main(["ezo", "cal", "-c", config, "ph", "clear"]) == 2
    assert "repeat with --yes" in capsys.readouterr().err
    assert ports["ph"].sent == []

    assert cli.main(["ezo", "cal", "-c", config, "ph", "clear", "--yes"]) == 0
    assert "cal clear" in ports["ph"].sent


def test_cal_refuses_a_circuit_of_the_wrong_type(config: str, ports, capsys):
    ports["ph"].reports = "orp"
    assert cli.main(["ezo", "cal", "-c", config, "ph", "mid", "7"]) == 1
    assert not any(s.startswith("cal") for s in ports["ph"].sent)
    assert "the circuit reports 'orp'" in capsys.readouterr().out


def test_cal_rejects_a_bad_point_without_sending(config: str, ports, capsys):
    assert cli.main(["ezo", "cal", "-c", config, "ph", "dry"]) == 2
    assert "ph has no calibration point 'dry'" in capsys.readouterr().err
    assert not any(s.startswith("cal") for s in ports["ph"].sent)


def test_cal_reports_a_rejected_command(config: str, ports, capsys):
    ports["do"].ack = Outcome.FAIL
    assert cli.main(["ezo", "cal", "-c", config, "do", "zero"]) == 1
    assert "error: do reported failed" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["heater", "missing"])
def test_cal_needs_an_ezo_device(config: str, ports, capsys, name: str):
    assert cli.main(["ezo", "cal", "-c", config, name, "status"]) == 1
    assert f"{name!r} is not an EZO device" in capsys.readouterr().err


def test_read_needs_an_ezo_device(tmp_path: Path, ports, capsys):
    path = tmp_path / "slices.toml"
    path.write_text(CONFIG.split('[[device]]\nname = "vessel_temp"')[0])
    assert cli.main(["read", "--once", "-c", str(path)]) == 1
    assert "lists no EZO devices" in capsys.readouterr().err


def test_read_once_says_when_compensation_had_no_temperature(config: str, ports, capsys):
    ports["vessel_temp"].replies.append(Outcome.FAIL)
    assert cli.main(["read", "--once", "-c", config]) == 1
    out = capsys.readouterr().out
    assert "vessel_temp              failed" in out
    assert "ph                            6.980 pH  (no RTD temperature" in out


def test_cal_mid_warns_that_it_clears_the_other_points(config: str, ports, capsys):
    assert cli.main(["ezo", "cal", "-c", config, "ph", "mid", "7"]) == 0
    assert "clears the low and high points" in capsys.readouterr().err
    assert cli.main(["ezo", "cal", "-c", config, "ph", "low", "4"]) == 0
    assert "clears" not in capsys.readouterr().err


def test_cal_rejects_nan(config: str, ports, capsys):
    assert cli.main(["ezo", "cal", "-c", config, "ph", "mid", "nan"]) == 2
    assert "finite" in capsys.readouterr().err
    assert not any(s.startswith("cal") for s in ports["ph"].sent)


def test_device_commands_refuse_while_another_controller_holds_the_lock(
    config: str, ports, lock: Path, capsys
):
    with ControllerLock(lock):
        assert cli.main(["read", "-c", config]) == 1
        assert cli.main(["ezo", "cal", "-c", config, "ph", "status"]) == 1
    err = capsys.readouterr().err
    holder = f"another controller holds {lock} ({getpass.getuser()}, pid {os.getpid()}"
    assert err.count(holder) == 2
    assert all(p.sent == [] for p in ports.values())


def test_the_lock_is_released_after_a_command(config: str, ports, lock: Path):
    assert cli.main(["read", "-c", config]) == 0
    with ControllerLock(lock):
        pass


def test_follow_sends_stop_all_on_sigterm_before_closing_the_circuits(
    config: str, ports, monkeypatch: pytest.MonkeyPatch, capsys
):
    order: list[str] = []
    for name, port in ports.items():
        monkeypatch.setattr(port, "close", lambda n=name: order.append(f"close {n}"))
    monkeypatch.setattr(cli, "actuators", lambda c: [FakeActuator("heater", order)])
    clock = cli.clock
    assert isinstance(clock, FakeClock)

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if clock.now > 5.0:
            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(cli, "sleep", sleep)

    assert cli.main(["read", "--follow", "-c", config]) == 0

    out = capsys.readouterr().out
    assert out.count("ph                            6.980 pH") >= 2  # cycles at 0, 2 and 4 s
    assert "stop-all heater: ok" in out
    assert order[0] == "safe heater"
    assert sorted(order[1:]) == ["close do", "close ph", "close vessel_temp"]
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


def test_sigterm_during_calibration_sends_no_further_command(
    config: str, ports, monkeypatch: pytest.MonkeyPatch, capsys
):
    clock = cli.clock
    assert isinstance(clock, FakeClock)
    port = ports["do"]

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if "temperature 20.0" in port.sent:
            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(cli, "sleep", sleep)

    assert cli.main(["ezo", "cal", "-c", config, "do", "zero"]) == 130
    assert port.sent[-1] == "temperature 20.0"
    assert not any(s.startswith("cal") for s in port.sent)
    assert "interrupted" in capsys.readouterr().err


def test_ctrl_c_during_read_once_exits_130(
    config: str, ports, monkeypatch: pytest.MonkeyPatch, capsys
):
    clock = cli.clock
    assert isinstance(clock, FakeClock)

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if clock.now > 0.5:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "sleep", sleep)
    assert cli.main(["read", "-c", config]) == 130
    assert all(p.closed for p in ports.values())


def test_sigterm_arriving_inside_the_printer_still_stops_follow(
    config: str, ports, monkeypatch: pytest.MonkeyPatch
):
    # Listener errors are isolated so a broken printer cannot stop control;
    # SIGTERM must not be mistaken for one.
    real = cli._print  # pyright: ignore[reportPrivateUsage]
    sent = {"done": False}

    def printer(item):
        if not sent["done"]:
            sent["done"] = True
            os.kill(os.getpid(), signal.SIGTERM)
        real(item)

    monkeypatch.setattr(cli, "_print", printer)
    clock = cli.clock
    assert isinstance(clock, FakeClock)

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        assert clock.now < 30, "follow kept running after SIGTERM"

    monkeypatch.setattr(cli, "sleep", sleep)
    assert cli.main(["read", "--follow", "-c", config]) == 0


def test_follow_stops_quietly_when_its_reader_goes_away(
    config: str, ports, monkeypatch: pytest.MonkeyPatch
):
    printed = {"n": 0}
    real = cli._print  # pyright: ignore[reportPrivateUsage]

    def fragile(item):
        printed["n"] += 1
        if printed["n"] > 2:
            raise BrokenPipeError
        real(item)

    monkeypatch.setattr(cli, "_print", fragile)
    dup2: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "dup2", lambda a, b: dup2.append((a, b)))
    assert cli.main(["read", "--follow", "-c", config]) == 0
    assert dup2 and dup2[0][1] == sys.stdout.fileno()
    assert all(p.closed for p in ports.values())


def test_once_and_follow_are_exclusive(config: str, ports, capsys):
    with pytest.raises(SystemExit):
        cli.main(["read", "--once", "--follow", "-c", config])


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "openreactor" / "openreactor.db"


def stop_after(seconds: float, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = cli.clock
    assert isinstance(clock, FakeClock)

    def sleep(s: float) -> None:
        clock.sleep(s)
        if clock.now > seconds:
            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(cli, "sleep", sleep)


def test_run_records_until_stopped_and_exports(
    config: str, ports, database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    monkeypatch.setattr(cli, "actuators", lambda c: [FakeActuator("heater")])
    stop_after(5.0, monkeypatch)

    assert cli.main(["run", "-c", config, "--name", "brew", "--notes", "first"]) == 0
    err = capsys.readouterr().err
    assert "run 1 (brew): recording" in err and "run 1: stopped" in err

    assert cli.main(["runs", "-c", config]) == 0
    assert capsys.readouterr().out.split()[:2] == ["1", "stopped"]

    out = tmp_path / "brew.zip"
    assert cli.main(["export", "-c", config, "1", "-o", str(out)]) == 0
    with zipfile.ZipFile(out) as z:
        readings = list(csv.reader(io.StringIO(z.read("readings.csv").decode())))
        events = z.read("events.csv").decode()
        meta = json.loads(z.read("run.json"))
    channels = {row[1] for row in readings[1:]}
    # The EZO circuits, and the RLHT's channel from its poll.
    assert channels == {
        "vessel_temp",
        "ph",
        "do.mg_l",
        "do.saturation",
        "jacket.temperature",
        "jacket.setpoint",
        "jacket.duty",
    }
    assert "stop-all,heater,,,ok" in events  # stop-all is part of the run
    assert meta["status"] == "stopped" and meta["notes"] == "first"
    assert meta["config"] == Path(config).read_text()


def test_a_run_left_running_is_marked_interrupted_by_the_next(
    config: str, ports, database: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    from openreactor.storage import Store

    store = Store(database)
    stale = store.start_run("crashed", "c")
    store.close()
    stop_after(5.0, monkeypatch)  # after the 1.8 s startup check

    assert cli.main(["run", "-c", config, "--name", "next"]) == 0
    assert f"run {stale} was left running; marked interrupted" in capsys.readouterr().err
    store = Store(database)
    assert [r.status for r in store.runs()] == ["interrupted", "stopped"]
    store.close()


def test_run_does_not_touch_the_database_while_another_controller_runs(
    config: str, ports, database: Path, lock: Path
):
    from openreactor.storage import Store

    store = Store(database)
    live = store.start_run("live", "c")
    store.close()
    with ControllerLock(lock):
        assert cli.main(["run", "-c", config, "--name", "second"]) == 1
    store = Store(database)
    assert [(r.id, r.status) for r in store.runs()] == [(live, "running")]
    store.close()


def test_export_of_an_unknown_run_fails_cleanly(config: str, database: Path, capsys):
    assert cli.main(["export", "-c", config, "9"]) == 1
    assert "no runs have been recorded yet" in capsys.readouterr().err
    from openreactor.storage import Store

    Store(database).close()
    assert cli.main(["export", "-c", config, "9"]) == 1
    assert "there is no run 9" in capsys.readouterr().err


def test_run_exits_1_when_recording_failed(
    config: str, ports, database: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    from openreactor import storage

    real = storage.Store.add_readings
    calls = {"n": 0}

    def failing(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 2:
            raise sqlite3.OperationalError("database or disk is full")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(storage.Store, "add_readings", failing)
    stop_after(5.0, monkeypatch)
    assert cli.main(["run", "-c", config, "--name", "brew"]) == 1
    assert "run 1: interrupted (database or disk is full)" in capsys.readouterr().err


def test_run_interrupted_before_it_starts_exits_130(
    config: str, ports, database: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    stop_after(0.5, monkeypatch)  # during the startup check
    assert cli.main(["run", "-c", config, "--name", "brew"]) == 130
    assert "interrupted before the run started" in capsys.readouterr().err
    assert not database.exists()


def test_a_long_database_lock_still_ends_the_run_interrupted(
    config: str, ports, database: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """A lock held past the recorder's short wait fails the write; the run
    is then marked interrupted when it ends, outside the tick."""
    clock = cli.clock
    assert isinstance(clock, FakeClock)
    state: dict[str, sqlite3.Connection | None] = {"other": None}

    def sleep(s: float) -> None:
        clock.sleep(s)
        if clock.now > 3.0 and state["other"] is None:
            other = sqlite3.connect(database, isolation_level=None)
            other.execute("BEGIN IMMEDIATE")
            state["other"] = other
        if clock.now > 6.0:
            other = state["other"]
            if other is not None and other.in_transaction:
                other.execute("ROLLBACK")
            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(cli, "sleep", sleep)
    assert cli.main(["run", "-c", config, "--name", "brew"]) == 1
    assert "run 1: interrupted (database is locked)" in capsys.readouterr().err
    from openreactor.storage import Store

    store = Store(database)
    assert [r.status for r in store.runs()] == ["interrupted"]
    store.close()


def test_read_and_ezo_cal_never_touch_a_slice(config: str, ports, monkeypatch, capsys):
    """Only run and serve start slices: a sensor read must not arm a
    watchdog or send a slice anything."""

    def no_bus(path: str) -> FakeI2cBus:
        raise AssertionError(f"opened {path}")

    monkeypatch.setattr(cli, "open_bus", no_bus)
    assert cli.main(["read", "--once", "-c", config]) == 0
    assert cli.main(["ezo", "cal", "-c", config, "ph", "status"]) == 0


def test_run_starts_the_slice_and_makes_it_safe_on_the_way_out(
    config: str, ports, heater: FakeRlht, database: Path, monkeypatch
):
    stop_after(3.0, monkeypatch)
    assert cli.main(["run", "-c", config, "--name", "brew"]) == 0
    sent = [op for op, _ in heater.commands]
    # Start-up: safe state, then the watchdog; at the end, stop-all.
    assert sent[:3] == [0x02, 0x06, 0x7E]
    assert sent[-2:] == [0x02, 0x06]
    assert heater.armed == 1
