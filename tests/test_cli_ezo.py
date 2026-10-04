from pathlib import Path

import pytest
from fakes import FakeClock, FakePort

from openreactor import cli
from openreactor.config import DeviceConfig
from openreactor.ezo import EzoDeviceError, Outcome, Value

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
