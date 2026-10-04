import pytest
from fakes import FakeClock, FakePort

from openreactor.config import DeviceConfig
from openreactor.ezo import (
    FAMILIES,
    EzoChannel,
    EzoDeviceError,
    EzoReader,
    EzoStatusError,
    Outcome,
    Value,
    _to_celsius,  # pyright: ignore[reportPrivateUsage]
    bus_number,
    calibrate,
    calibration_status,
    clear_calibration,
)

RTD = (Value("", 25.0, "°C"),)
PH = (Value("", 7.0, "pH"),)
ORP = (Value("", 225.0, "mV"),)


def device(name: str, kind: str, address: int, temp_comp: str | None = None) -> DeviceConfig:
    return DeviceConfig(name, kind, "/dev/i2c-1", address, temp_comp=temp_comp)


def reader(*pairs: tuple[DeviceConfig, FakePort]) -> tuple[EzoReader, FakeClock]:
    clock = FakeClock()
    channels = [EzoChannel(d, p) for d, p in pairs]
    return EzoReader(channels, clock=clock, sleep=clock.sleep), clock


def by_channel(results):
    return {r.channel: r for r in results}


# Reading


def test_split_phase_sends_every_read_before_reading_any():
    clock = FakeClock()
    trace: list[tuple[str, float]] = []
    ph = FakePort("ph", PH, wait_ms=900, trace=trace, clock=clock)
    orp = FakePort("orp", ORP, wait_ms=1000, trace=trace, clock=clock)
    r = EzoReader(
        [
            EzoChannel(device("ph", "ezo-ph", 0x63), ph),
            EzoChannel(device("orp", "ezo-orp", 0x62), orp),
        ],
        clock=clock,
        sleep=clock.sleep,
    )

    results = r.run_cycle()

    # Each circuit is read when its own delay is up, and the slowest one
    # sets the cycle time: the waits do not add up.
    assert trace == [
        ("send ph", 0.0),
        ("send orp", 0.0),
        ("read ph", pytest.approx(0.9)),
        ("read orp", pytest.approx(1.0)),
    ]
    assert by_channel(results)["ph"].values == PH
    assert by_channel(results)["orp"].values == ORP


def test_cached_output_mask_is_passed_to_every_read():
    port = FakePort("do", (Value("mg_l", 8.1, "mg/L"),), config=0b11)
    r, _ = reader((device("do", "ezo-do", 0x61), port))
    assert r.prepare() == []
    r.run_cycle()
    r.run_cycle()
    assert port.configs_used == [0b11, 0b11]


# Temperature compensation


def test_read_once_compensates_with_this_readings_rtd_temperature():
    rtd, ph = FakePort("rtd", RTD), FakePort("ph", PH)
    r, _ = reader(
        (device("t", "ezo-rtd", 0x66), rtd), (device("ph", "ezo-ph", 0x63, temp_comp="t"), ph)
    )

    results = by_channel(r.read_once())

    assert ph.read_temperatures == [25.0]
    assert rtd.read_temperatures == [None]
    assert results["ph"].temperature_c == 25.0
    assert results["t"].temperature_c is None


def test_a_cycle_compensates_with_the_last_rtd_value():
    rtd, ph = FakePort("rtd", RTD), FakePort("ph", PH)
    rtd.replies.extend([RTD, (Value("", 30.0, "°C"),)])
    r, _ = reader(
        (device("t", "ezo-rtd", 0x66), rtd), (device("ph", "ezo-ph", 0x63, temp_comp="t"), ph)
    )

    r.run_cycle()  # no RTD value yet
    r.run_cycle()  # sent before this cycle's RTD reading arrives

    assert ph.read_temperatures == [None, 25.0]


def test_no_compensation_without_temp_comp():
    rtd, ph = FakePort("rtd", RTD), FakePort("ph", PH)
    r, _ = reader((device("t", "ezo-rtd", 0x66), rtd), (device("ph", "ezo-ph", 0x63), ph))
    r.read_once()
    r.run_cycle()
    assert ph.read_temperatures == [None, None]


# NOT_READY, FAIL and NO_DATA


def test_not_ready_is_read_again_next_cycle_without_a_new_read():
    port = FakePort("ph", PH)
    port.replies.append(Outcome.NOT_READY)
    r, _ = reader((device("ph", "ezo-ph", 0x63), port))

    results = r.run_cycle()

    assert port.sent.count("read") == 1
    assert len(port.configs_used) == 2
    assert [(x.outcome, x.values) for x in results] == [(Outcome.OK, PH)]


def test_not_ready_twice_is_reported():
    port = FakePort("ph", PH)
    port.replies.extend([Outcome.NOT_READY, Outcome.NOT_READY])
    r, _ = reader((device("ph", "ezo-ph", 0x63), port))

    results = r.run_cycle()

    assert [x.outcome for x in results] == [Outcome.NOT_READY]
    assert port.sent.count("read") == 1
    # The next cycle starts afresh.
    assert r.run_cycle()[0].outcome is Outcome.OK
    assert port.sent.count("read") == 2


@pytest.mark.parametrize("outcome", [Outcome.FAIL, Outcome.NO_DATA])
def test_fail_and_no_data_are_reported_without_a_retry(outcome: Outcome):
    port, other = FakePort("ph", PH), FakePort("orp", ORP)
    port.replies.append(outcome)
    r, _ = reader((device("ph", "ezo-ph", 0x63), port), (device("orp", "ezo-orp", 0x62), other))

    results = by_channel(r.run_cycle())

    assert results["ph"].outcome is outcome
    assert results["orp"].outcome is Outcome.OK
    assert len(port.configs_used) == 1


def test_a_failed_send_is_reported_and_others_still_read():
    port, other = FakePort("ph", PH), FakePort("orp", ORP)
    port.send_error = EzoDeviceError("transport failure")
    r, _ = reader((device("ph", "ezo-ph", 0x63), port), (device("orp", "ezo-orp", 0x62), other))

    results = by_channel(r.run_cycle())

    assert results["ph"].outcome is Outcome.ERROR
    assert results["ph"].detail == "transport failure"
    assert results["orp"].outcome is Outcome.OK


# Type check at startup


def test_prepare_accepts_the_configured_family():
    port = FakePort("ph", PH)
    r, _ = reader((device("ph", "ezo-ph", 0x63), port))
    assert r.prepare() == []
    assert port.sent == ["info", "config"]


def test_prepare_disables_a_circuit_of_the_wrong_type():
    wrong, right = FakePort("ph", PH, reports="orp"), FakePort("orp", ORP)
    r, _ = reader((device("ph", "ezo-ph", 0x63), wrong), (device("orp", "ezo-orp", 0x62), right))

    problems = r.prepare()

    assert len(problems) == 1
    assert problems[0].channel == "ph"
    assert problems[0].outcome is Outcome.ERROR
    assert problems[0].detail == "config says ezo-ph, the circuit reports 'orp'"
    results = by_channel(r.run_cycle())
    assert "ph" not in results
    assert "read" not in wrong.sent
    assert results["orp"].outcome is Outcome.OK


def test_prepare_reports_a_circuit_that_does_not_answer():
    port = FakePort("ph", PH)
    port.family_error = EzoStatusError(Outcome.NO_DATA)
    r, _ = reader((device("ph", "ezo-ph", 0x63), port))

    [problem] = r.prepare()

    assert problem.outcome is Outcome.NO_DATA


# Calibration


@pytest.mark.parametrize(
    ("kind", "point", "value"),
    [
        ("ezo-ph", "mid", 7.0),
        ("ezo-ph", "low", 4.0),
        ("ezo-ph", "high", 10.0),
        ("ezo-orp", "ref", 225.0),
        ("ezo-rtd", "ref", 100.0),
        ("ezo-ec", "dry", None),
        ("ezo-ec", "single", 1413.0),
        ("ezo-ec", "low", 12880.0),
        ("ezo-ec", "high", 80000.0),
        ("ezo-do", "atmospheric", None),
        ("ezo-do", "zero", None),
        ("ezo-hum", "temperature", 21.5),
    ],
)
def test_each_calibration_point_is_sent(kind: str, point: str, value: float | None):
    family = FAMILIES[kind]
    port = FakePort(family.name)
    clock = FakeClock()

    calibrate(family, port, point, value, clock.sleep)

    assert port.sent == [f"cal {point} {value}"]
    assert clock.sleeps == [0.9]
    assert calibration_status(port, clock.sleep) == f"calibrated at {point}"


def test_every_family_point_is_covered_above():
    covered = (
        {("ezo-ph", p) for p in ("mid", "low", "high")}
        | {("ezo-orp", "ref"), ("ezo-rtd", "ref"), ("ezo-hum", "temperature")}
        | {("ezo-ec", p) for p in ("dry", "single", "low", "high")}
        | {("ezo-do", p) for p in ("atmospheric", "zero")}
    )
    assert covered == {(k, p) for k, f in FAMILIES.items() for p in f.points}


def test_calibration_rejects_an_unknown_point():
    port = FakePort("ph")
    with pytest.raises(ValueError, match="has no calibration point 'dry'"):
        calibrate(FAMILIES["ezo-ph"], port, "dry", None)
    assert port.sent == []


def test_calibration_point_needs_its_value():
    port = FakePort("ph")
    with pytest.raises(ValueError, match="needs a reference value"):
        calibrate(FAMILIES["ezo-ph"], port, "mid", None)
    with pytest.raises(ValueError, match="takes no reference value"):
        calibrate(FAMILIES["ezo-do"], port, "zero", 1.0)
    assert port.sent == []


def test_a_rejected_calibration_raises():
    port = FakePort("ph")
    port.ack = Outcome.FAIL
    with pytest.raises(EzoStatusError) as e:
        calibrate(FAMILIES["ezo-ph"], port, "mid", 7.0, FakeClock().sleep)
    assert e.value.outcome is Outcome.FAIL


def test_clear_calibration():
    port = FakePort("ph")
    port.calibration = "two point"
    clock = FakeClock()
    clear_calibration(port, clock.sleep)
    assert port.sent == ["cal clear"]
    assert calibration_status(port, clock.sleep) == "not calibrated"


# Helpers


def test_bus_number():
    assert bus_number("/dev/i2c-1") == 1
    assert bus_number("/dev/i2c-22") == 22
    with pytest.raises(ValueError):
        bus_number("/dev/i2c-1x")


@pytest.mark.parametrize(
    ("value", "scale", "celsius"),
    [(25.0, "CELSIUS", 25.0), (298.15, "KELVIN", 25.0), (77.0, "FAHRENHEIT", 25.0)],
)
def test_rtd_temperature_is_converted_to_celsius(value: float, scale: str, celsius: float):
    assert _to_celsius(value, scale) == pytest.approx(celsius)
