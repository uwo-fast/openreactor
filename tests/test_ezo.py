from types import SimpleNamespace

import ezo_driver
import pytest
from ezo_driver import enums
from fakes import FakeClock, FakePort

from openreactor.config import DeviceConfig
from openreactor.controller import Controller
from openreactor.ezo import (
    FAMILIES,
    EzoChannel,
    EzoDeviceError,
    EzoReader,
    EzoStatusError,
    Outcome,
    Value,
    bus_number,
    calibrate,
    calibration_args,
    calibration_status,
    calibration_text,
    clear_calibration,
    outcome_for,
    to_celsius,
    values_from_reading,
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


def _controller(r: EzoReader) -> Controller:
    # The reader's own fake clock drives the controller too.
    return Controller(r, ezo_period_s=2.0, clock=r._clock, sleep=r._sleep)  # pyright: ignore[reportPrivateUsage]


def cycle(r: EzoReader):
    c = _controller(r)
    return c.wait(c.read_cycle())


def once(r: EzoReader):
    return _controller(r).read_once()


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

    results = cycle(r)

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
    cycle(r)
    cycle(r)
    assert port.configs_used == [0b11, 0b11]


# Temperature compensation


def test_read_once_compensates_with_this_readings_rtd_temperature():
    rtd, ph = FakePort("rtd", RTD), FakePort("ph", PH)
    r, _ = reader(
        (device("t", "ezo-rtd", 0x66), rtd), (device("ph", "ezo-ph", 0x63, temp_comp="t"), ph)
    )

    results = by_channel(once(r))

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

    cycle(r)  # no RTD value yet
    cycle(r)  # sent before this cycle's RTD reading arrives

    assert ph.read_temperatures == [None, 25.0]


def test_no_compensation_without_temp_comp():
    rtd, ph = FakePort("rtd", RTD), FakePort("ph", PH)
    r, _ = reader((device("t", "ezo-rtd", 0x66), rtd), (device("ph", "ezo-ph", 0x63), ph))
    once(r)
    cycle(r)
    assert ph.read_temperatures == [None, None]


def test_a_failed_rtd_read_clears_its_temperature():
    rtd, ph = FakePort("rtd", RTD), FakePort("ph", PH)
    rtd.replies.extend([RTD, Outcome.FAIL])
    r, _ = reader(
        (device("t", "ezo-rtd", 0x66), rtd), (device("ph", "ezo-ph", 0x63, temp_comp="t"), ph)
    )

    once(r)  # RTD 25.0, pH compensated at 25.0
    cycle(r)  # pH still sent 25.0; the RTD fails
    third = by_channel(cycle(r))

    assert ph.read_temperatures == [25.0, 25.0, None]
    assert third["ph"].compensation_missing
    assert third["ph"].temperature_c is None


def test_compensation_missing_only_for_channels_that_compensate():
    rtd, ph, orp = FakePort("rtd", RTD), FakePort("ph", PH), FakePort("orp", ORP)
    rtd.replies.append(Outcome.NO_DATA)
    r, _ = reader(
        (device("t", "ezo-rtd", 0x66), rtd),
        (device("ph", "ezo-ph", 0x63, temp_comp="t"), ph),
        (device("orp", "ezo-orp", 0x62), orp),
    )

    results = by_channel(once(r))

    assert results["ph"].compensation_missing
    assert not results["orp"].compensation_missing
    assert not results["t"].compensation_missing


# NOT_READY, FAIL and NO_DATA


def traced(*specs: tuple[str, str, int, tuple[Value, ...]]):
    clock = FakeClock()
    trace: list[tuple[str, float]] = []
    pairs = []
    for name, kind, address, values in specs:
        family = FAMILIES[kind].name
        pairs.append(
            (device(name, kind, address), FakePort(family, values, trace=trace, clock=clock))
        )
    r = EzoReader([EzoChannel(d, p) for d, p in pairs], clock=clock, sleep=clock.sleep)
    return r, [p for _, p in pairs], trace


def test_not_ready_is_read_again_a_full_delay_later_without_a_new_read():
    r, (ph, orp), trace = traced(("ph", "ezo-ph", 0x63, PH), ("orp", "ezo-orp", 0x62, ORP))
    ph.replies.append(Outcome.NOT_READY)

    results = cycle(r)

    assert trace == [
        ("send ph", 0.0),
        ("send orp", 0.0),
        ("read ph", pytest.approx(0.9)),
        ("read orp", pytest.approx(0.9)),
        # Retried one pH delay later, with no new read sent to either circuit.
        ("read ph", pytest.approx(1.8)),
    ]
    assert sorted((x.channel, x.outcome) for x in results) == [
        ("orp", Outcome.OK),
        ("ph", Outcome.OK),
    ]
    assert r.next_due() is None


def test_not_ready_twice_is_reported_and_nothing_is_left_pending():
    r, (ph, orp), _ = traced(("ph", "ezo-ph", 0x63, PH), ("orp", "ezo-orp", 0x62, ORP))
    ph.replies.extend([Outcome.NOT_READY, Outcome.NOT_READY])

    results = cycle(r)

    assert sorted((x.channel, x.outcome) for x in results) == [
        ("orp", Outcome.OK),
        ("ph", Outcome.NOT_READY),
    ]
    assert r.next_due() is None
    # The next cycle starts afresh with one new read each.
    assert sorted((x.channel, x.outcome) for x in cycle(r)) == [
        ("orp", Outcome.OK),
        ("ph", Outcome.OK),
    ]
    assert ph.sent.count("read") == 2
    assert orp.sent.count("read") == 2


def test_a_circuit_that_is_never_ready_does_not_duplicate_the_others():
    r, (ph, orp), _ = traced(("ph", "ezo-ph", 0x63, PH), ("orp", "ezo-orp", 0x62, ORP))
    ph.replies.extend([Outcome.NOT_READY] * 6)

    for _ in range(3):
        results = cycle(r)
        assert sorted((x.channel, x.outcome) for x in results) == [
            ("orp", Outcome.OK),
            ("ph", Outcome.NOT_READY),
        ]

    assert ph.sent.count("read") == 3
    assert orp.sent.count("read") == 3


@pytest.mark.parametrize("outcome", [Outcome.FAIL, Outcome.NO_DATA])
def test_fail_and_no_data_are_reported_without_a_retry(outcome: Outcome):
    port, other = FakePort("ph", PH), FakePort("orp", ORP)
    port.replies.append(outcome)
    r, _ = reader((device("ph", "ezo-ph", 0x63), port), (device("orp", "ezo-orp", 0x62), other))

    results = by_channel(cycle(r))

    assert results["ph"].outcome is outcome
    assert results["orp"].outcome is Outcome.OK
    assert len(port.configs_used) == 1


def test_a_failed_send_is_reported_and_others_still_read():
    port, other = FakePort("ph", PH), FakePort("orp", ORP)
    port.send_error = EzoDeviceError("transport failure")
    r, _ = reader((device("ph", "ezo-ph", 0x63), port), (device("orp", "ezo-orp", 0x62), other))

    results = by_channel(cycle(r))

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
    results = by_channel(cycle(r))
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

    # EC and DO calibrate at their default compensation temperature, which a
    # compensated read may have changed.
    reset = {"ec": ["temperature 25.0"], "do": ["temperature 20.0"]}.get(family.name, [])
    assert port.sent == [*reset, f"cal {point} {value}"]
    assert clock.sleeps == [0.3] * len(reset) + [0.9]
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


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_calibration_rejects_a_non_finite_value(value: float):
    port = FakePort("ph")
    with pytest.raises(ValueError, match="finite"):
        calibrate(FAMILIES["ezo-ph"], port, "mid", value)
    assert port.sent == []


def test_a_rejected_temperature_reset_stops_the_calibration():
    port = FakePort("ec")
    port.ack = Outcome.FAIL
    with pytest.raises(EzoStatusError):
        calibrate(FAMILIES["ezo-ec"], port, "single", 1413.0, FakeClock().sleep)
    assert port.sent == ["temperature 25.0"]


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
    assert to_celsius(value, scale) == pytest.approx(celsius)


# ezo-driver translation, checked against the installed ezo-driver's enums


def test_outcome_for_maps_each_non_success_status():
    s = ezo_driver.DeviceStatus
    assert outcome_for(s.NOT_READY, s) is Outcome.NOT_READY
    assert outcome_for(s.FAIL, s) is Outcome.FAIL
    assert outcome_for(s.NO_DATA, s) is Outcome.NO_DATA
    assert outcome_for(s.UNKNOWN, s) is None
    assert outcome_for(s.SUCCESS, s) is None


def test_single_output_values():
    assert values_from_reading("ph", SimpleNamespace(ph=7.02), enums) == (Value("", 7.02, "pH"),)
    assert values_from_reading("orp", SimpleNamespace(millivolts=-50.0), enums) == (
        Value("", -50.0, "mV"),
    )
    rtd = SimpleNamespace(temperature=77.0, scale=enums.RTDScale.FAHRENHEIT)
    [v] = values_from_reading("rtd", rtd, enums)
    assert (v.field, v.unit) == ("", "°C")
    assert v.value == pytest.approx(25.0)


def test_rtd_without_a_probe_is_an_error():
    rtd = SimpleNamespace(temperature=-1023.0, scale=enums.RTDScale.CELSIUS)
    with pytest.raises(EzoDeviceError, match="no RTD probe"):
        values_from_reading("rtd", rtd, enums)


def test_multi_output_values_follow_the_mask():
    ec = SimpleNamespace(
        present_mask=enums.ECOutputMask.CONDUCTIVITY | enums.ECOutputMask.SALINITY,
        conductivity_us_cm=842.0,
        total_dissolved_solids_ppm=421.0,
        salinity_ppt=1.021,
        specific_gravity=1.0,
    )
    assert values_from_reading("ec", ec, enums) == (
        Value("conductivity", 842.0, "µS/cm"),
        Value("salinity", 1.021, "ppt"),
    )
    do = SimpleNamespace(
        present_mask=enums.DOOutputMask.PERCENT_SATURATION,
        milligrams_per_liter=8.5,
        percent_saturation=94.2,
    )
    assert values_from_reading("do", do, enums) == (Value("saturation", 94.2, "%"),)
    hum = SimpleNamespace(
        present_mask=enums.HUMOutputMask.HUMIDITY | enums.HUMOutputMask.DEW_POINT,
        relative_humidity_percent=44.0,
        air_temperature_c=21.8,
        dew_point_c=8.1,
    )
    assert values_from_reading("hum", hum, enums) == (
        Value("humidity", 44.0, "%"),
        Value("dew_point", 8.1, "°C"),
    )


@pytest.mark.parametrize(
    ("family", "status", "text"),
    [
        ("ph", SimpleNamespace(level=0), "not calibrated"),
        ("ph", SimpleNamespace(level=3), "three point"),
        ("ec", SimpleNamespace(level=1), "two point"),
        ("ec", SimpleNamespace(level=2), "three point"),
        ("do", SimpleNamespace(level=1), "one point"),
        ("do", SimpleNamespace(level=9), "calibration level 9"),
        ("orp", SimpleNamespace(calibrated=True), "calibrated"),
        ("rtd", SimpleNamespace(calibrated=False), "not calibrated"),
        ("hum", SimpleNamespace(calibrated=True), "temperature calibrated"),
    ],
)
def test_calibration_text(family: str, status: SimpleNamespace, text: str):
    assert calibration_text(family, status) == text


@pytest.mark.parametrize(
    ("family", "point", "value", "args"),
    [
        ("ph", "mid", 7.0, (enums.PHCalibrationPoint.MID, 7.0)),
        ("ph", "low", 4.0, (enums.PHCalibrationPoint.LOW, 4.0)),
        ("ph", "high", 10.0, (enums.PHCalibrationPoint.HIGH, 10.0)),
        ("orp", "ref", -50.0, (-50.0,)),
        ("rtd", "ref", 100.0, (100.0,)),
        ("ec", "dry", None, (enums.ECCalibrationPoint.DRY, 0.0)),
        ("ec", "single", 1413.0, (enums.ECCalibrationPoint.SINGLE_POINT, 1413.0)),
        ("ec", "low", 12880.0, (enums.ECCalibrationPoint.LOW_POINT, 12880.0)),
        ("ec", "high", 80000.0, (enums.ECCalibrationPoint.HIGH_POINT, 80000.0)),
        ("do", "atmospheric", None, (enums.DOCalibrationPoint.ATMOSPHERIC,)),
        ("do", "zero", None, (enums.DOCalibrationPoint.ZERO,)),
        ("hum", "temperature", 21.5, (21.5,)),
    ],
)
def test_calibration_args(family: str, point: str, value: float | None, args: tuple):
    assert calibration_args(family, point, value, enums) == args


def test_calibration_args_cover_every_point():
    for family in FAMILIES.values():
        for point, needs_value in family.points.items():
            calibration_args(family.name, point, 1.0 if needs_value else None, enums)
