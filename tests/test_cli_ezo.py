import os
import signal
from pathlib import Path

import pytest
from fakes import FakeClock, FakePort

from openreactor import cli
from openreactor.config import DeviceConfig
from openreactor.controller import Event
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
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # The controller lock lives in the per-user state directory by default.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path / "state" / "openreactor"


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
    config: str, ports, state: Path, capsys
):
    with ControllerLock(state):
        assert cli.main(["read", "-c", config]) == 1
        assert cli.main(["ezo", "cal", "-c", config, "ph", "status"]) == 1
    err = capsys.readouterr().err
    assert (
        err.count(f"another controller holds {state / 'openreactor.lock'} (pid {os.getpid()}") == 2
    )
    assert all(p.sent == [] for p in ports.values())


def test_the_lock_is_released_after_a_command(config: str, ports, state: Path):
    assert cli.main(["read", "-c", config]) == 0
    with ControllerLock(state):
        pass


def test_follow_sends_stop_all_on_sigterm_before_closing_the_circuits(
    config: str, ports, monkeypatch: pytest.MonkeyPatch, capsys
):
    order: list[str] = []
    for name, port in ports.items():
        monkeypatch.setattr(port, "close", lambda n=name: order.append(f"close {n}"))
    printed = cli._print  # pyright: ignore[reportPrivateUsage]

    def record(item):
        if isinstance(item, Event):
            order.append(item.kind)
        printed(item)

    monkeypatch.setattr(cli, "_print", record)
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
    assert order[0] == "stop-all"
    assert sorted(order[1:]) == ["close do", "close ph", "close vessel_temp"]
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


def test_once_and_follow_are_exclusive(config: str, ports, capsys):
    with pytest.raises(SystemExit):
        cli.main(["read", "--once", "--follow", "-c", config])
