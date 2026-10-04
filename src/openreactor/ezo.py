"""Atlas Scientific EZO circuits: identity check, split-phase reads with
temperature compensation, and calibration.

Each circuit is reached through an ``EzoPort``. ``DriverPort`` implements it
over ezo-driver on Linux I2C; tests use a fake. ``EzoReader`` sends every read,
then reads each circuit when its own delay is up, so one slow circuit never
holds up another.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Generator, Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, TypeVar

from openreactor.config import I2C_BUS, DeviceConfig


class Outcome(Enum):
    OK = "ok"
    NOT_READY = "not ready"
    FAIL = "failed"
    NO_DATA = "no data"
    ERROR = "error"


class EzoStatusError(Exception):
    """The circuit answered with a status other than success."""

    def __init__(self, outcome: Outcome):
        super().__init__(outcome.value)
        self.outcome = outcome


class EzoDeviceError(Exception):
    """The circuit could not be reached, or its reply could not be used."""


@dataclass(frozen=True)
class Value:
    """One output of a reading. ``field`` is empty for single-output families."""

    field: str
    value: float
    unit: str


@dataclass(frozen=True)
class Family:
    name: str
    temp_comp: bool
    # Calibration points, and whether each takes a reference value.
    points: dict[str, bool]
    # The compensation temperature the datasheet says to calibrate at. A
    # compensated read leaves its temperature set on the circuit, so it is
    # put back to this default before calibrating.
    calibration_temp_c: float | None = None


FAMILIES: dict[str, Family] = {
    "ezo-ph": Family("ph", True, {"mid": True, "low": True, "high": True}),
    "ezo-orp": Family("orp", False, {"ref": True}),
    "ezo-rtd": Family("rtd", False, {"ref": True}),
    "ezo-ec": Family("ec", True, {"dry": False, "single": True, "low": True, "high": True}, 25.0),
    "ezo-do": Family("do", True, {"atmospheric": False, "zero": False}, 20.0),
    "ezo-hum": Family("hum", False, {"temperature": True}),
}

_T = TypeVar("_T")

# What an EZO-RTD reads with no probe connected.
RTD_NO_PROBE = -1023.0


class EzoPort(Protocol):
    """One EZO circuit. Each ``send_*`` returns the wait in ms before its
    ``read_*`` may be called. Reads raise ``EzoStatusError`` when the circuit
    reports NOT_READY, FAIL or NO_DATA, and ``EzoDeviceError`` otherwise."""

    def send_info_query(self) -> int: ...
    def read_family(self) -> str: ...
    def send_config_query(self) -> int: ...
    def read_config(self) -> int | None: ...
    def send_read(self, temperature_c: float | None) -> int: ...
    def read_values(self, config: int | None) -> tuple[Value, ...]: ...
    def send_temperature(self, temperature_c: float) -> int: ...
    def send_calibration_query(self) -> int: ...
    def read_calibration_status(self) -> str: ...
    def send_calibration_clear(self) -> int: ...
    def send_calibration(self, point: str, value: float | None) -> int: ...
    def read_ack(self) -> None: ...
    def close(self) -> None: ...


def bus_number(bus: str) -> int:
    match = I2C_BUS.fullmatch(bus)
    if match is None:
        raise ValueError(f"{bus!r} is not a Linux I2C bus such as /dev/i2c-1")
    return int(match.group(1))


@dataclass
class _Pending:
    due: float
    wait_s: float
    temperature_c: float | None
    retried: bool = False
    # A NOT_READY reply waits for the next cycle before it is read again.
    waiting_for_cycle: bool = False


@dataclass(frozen=True)
class Result:
    channel: str
    outcome: Outcome
    values: tuple[Value, ...] = ()
    detail: str = ""
    # The temperature sent with this read for compensation, if any.
    temperature_c: float | None = None
    # The channel compensates from an RTD, but no RTD temperature was
    # available, so the circuit used the last temperature it was given.
    compensation_missing: bool = False


@dataclass
class EzoChannel:
    device: DeviceConfig
    port: EzoPort
    family: Family = field(init=False)
    config: int | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        self.family = FAMILIES[self.device.kind]

    @property
    def name(self) -> str:
        return self.device.name


def _celsius(values: Iterable[Value]) -> float | None:
    for v in values:
        if v.unit == "°C":
            return v.value
    return None


class EzoReader:
    """Split-phase reads across a set of circuits.

    ``begin`` sends a read to every circuit that has none outstanding, and
    re-arms a circuit that answered NOT_READY so it is read again one delay
    later. ``collect`` reads the circuits whose delay is up. Neither waits; the
    controller decides when to call them. pH, EC and DO
    compensate with the last temperature read from the RTD named in their
    ``temp_comp``; a failed RTD read clears that temperature.
    """

    def __init__(
        self,
        channels: Sequence[EzoChannel],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.channels = list(channels)
        self._clock = clock
        self._sleep = sleep
        self._pending: dict[str, _Pending] = {}
        self._temperatures: dict[str, float] = {}
        self._by_name = {c.name: c for c in self.channels}

    def prepare(self) -> list[Result]:
        """Check each circuit's type against the config and cache the output
        masks of DO, EC and HUM and the RTD scale. A circuit that fails is
        disabled. Blocks for each query's delay."""
        problems: list[Result] = []
        for ch in self.channels:
            try:
                self._sleep(ch.port.send_info_query() / 1000)
                family = ch.port.read_family()
                if family != ch.family.name:
                    raise EzoDeviceError(
                        f"config says {ch.device.kind}, the circuit reports {family!r}"
                    )
                self._sleep(ch.port.send_config_query() / 1000)
                ch.config = ch.port.read_config()
            except EzoStatusError as e:
                ch.enabled = False
                problems.append(Result(ch.name, e.outcome, detail="during the startup check"))
            except EzoDeviceError as e:
                ch.enabled = False
                problems.append(Result(ch.name, Outcome.ERROR, detail=str(e)))
        return problems

    def _temperature_for(self, ch: EzoChannel) -> float | None:
        # Config validation allows temp_comp only on pH, EC and DO.
        source = ch.device.temp_comp
        return self._temperatures.get(source) if source is not None else None

    def pending(self) -> set[str]:
        """Circuits with a read outstanding, including NOT_READY retries."""
        return set(self._pending)

    def waiting(self) -> set[str]:
        """Circuits that answered NOT_READY and wait to be re-armed."""
        return {name for name, p in self._pending.items() if p.waiting_for_cycle}

    def rearm(self) -> None:
        """Read again, one delay from now, every circuit that answered
        NOT_READY. ``begin`` does this at the start of each cycle."""
        self._rearm(self._clock())

    def _rearm(self, now: float) -> None:
        for pending in self._pending.values():
            if pending.waiting_for_cycle:
                pending.waiting_for_cycle = False
                pending.due = now + pending.wait_s

    def begin(self, names: Iterable[str] | None = None, skip: Iterable[str] = ()) -> list[Result]:
        """Start a cycle for ``names`` (default all), leaving out ``skip``.
        Returns the circuits whose read could not be sent."""
        now = self._clock()
        self._rearm(now)
        wanted = set(names) if names is not None else None
        skipped = set(skip)
        failed: list[Result] = []
        for ch in self.channels:
            if not ch.enabled or (wanted is not None and ch.name not in wanted):
                continue
            if ch.name in skipped:
                continue
            if ch.name in self._pending:
                continue
            temperature = self._temperature_for(ch)
            try:
                wait_s = ch.port.send_read(temperature) / 1000
            except (EzoStatusError, EzoDeviceError) as e:
                failed.append(Result(ch.name, Outcome.ERROR, detail=str(e)))
                self._forget_temperature(ch)
                continue
            self._pending[ch.name] = _Pending(now + wait_s, wait_s, temperature)
        return failed

    def next_due(self) -> float | None:
        dues = [p.due for p in self._pending.values() if not p.waiting_for_cycle]
        return min(dues) if dues else None

    def _forget_temperature(self, ch: EzoChannel) -> None:
        if ch.family.name == "rtd":
            self._temperatures.pop(ch.name, None)

    def _result(self, ch: EzoChannel, pending: _Pending, outcome: Outcome, **kw: Any) -> Result:
        missing = ch.device.temp_comp is not None and pending.temperature_c is None
        return Result(
            ch.name,
            outcome,
            temperature_c=pending.temperature_c,
            compensation_missing=missing,
            **kw,
        )

    def collect(self) -> list[Result]:
        """Read every circuit whose delay is up."""
        now = self._clock()
        results: list[Result] = []
        for name, pending in list(self._pending.items()):
            # A microsecond's grace, so a read due at a tick is read on it.
            if pending.waiting_for_cycle or pending.due > now + 1e-6:
                continue
            ch = self._by_name[name]
            try:
                values = ch.port.read_values(ch.config)
            except EzoStatusError as e:
                if e.outcome is Outcome.NOT_READY and not pending.retried:
                    pending.retried = True
                    pending.waiting_for_cycle = True
                    continue
                del self._pending[name]
                self._forget_temperature(ch)
                results.append(self._result(ch, pending, e.outcome))
                continue
            except EzoDeviceError as e:
                del self._pending[name]
                self._forget_temperature(ch)
                results.append(self._result(ch, pending, Outcome.ERROR, detail=str(e)))
                continue
            del self._pending[name]
            if ch.family.name == "rtd":
                celsius = _celsius(values)
                if celsius is not None:
                    self._temperatures[name] = celsius
            results.append(self._result(ch, pending, Outcome.OK, values=values))
        return results


# Multi-step commands are generators: each yields how long to wait, in
# seconds, before it may continue, and returns its result. run_steps drives
# one with sleep for the CLI; the controller resumes it on later ticks.
Steps = Generator[float, None, _T]


def run_steps(steps: Steps[_T], sleep: Callable[[float], None] = time.sleep) -> _T:
    try:
        while True:
            sleep(next(steps))
    except StopIteration as done:
        return done.value


def command_steps(port: EzoPort, send: Callable[[], int]) -> Steps[None]:
    """Send a command, wait its delay, and check the circuit accepted it."""
    yield send() / 1000
    port.read_ack()


def calibration_status_steps(port: EzoPort) -> Steps[str]:
    yield port.send_calibration_query() / 1000
    return port.read_calibration_status()


def check_calibration(family: Family, point: str, value: float | None) -> None:
    """Raise ValueError unless ``point`` and ``value`` suit ``family``."""
    if point not in family.points:
        raise ValueError(
            f"{family.name} has no calibration point {point!r}; "
            f"expected one of {', '.join(family.points)}"
        )
    needs_value = family.points[point]
    if needs_value and value is None:
        raise ValueError(f"calibration point {point!r} needs a reference value")
    if not needs_value and value is not None:
        raise ValueError(f"calibration point {point!r} takes no reference value")
    if value is not None and not math.isfinite(value):
        raise ValueError(f"the reference value must be a finite number, not {value}")


def calibrate_steps(family: Family, port: EzoPort, point: str, value: float | None) -> Steps[None]:
    """Calibrate one point. EC and DO are first put back to their default
    compensation temperature, as their datasheets require. The arguments are
    checked before anything is sent."""
    check_calibration(family, point, value)
    if family.calibration_temp_c is not None:
        temperature = family.calibration_temp_c
        yield from command_steps(port, lambda: port.send_temperature(temperature))
    yield from command_steps(port, lambda: port.send_calibration(point, value))


def clear_calibration_steps(port: EzoPort) -> Steps[None]:
    yield from command_steps(port, port.send_calibration_clear)


def calibration_status(port: EzoPort, sleep: Callable[[float], None] = time.sleep) -> str:
    return run_steps(calibration_status_steps(port), sleep)


def calibrate(
    family: Family,
    port: EzoPort,
    point: str,
    value: float | None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    run_steps(calibrate_steps(family, port, point, value), sleep)


def clear_calibration(port: EzoPort, sleep: Callable[[float], None] = time.sleep) -> None:
    run_steps(clear_calibration_steps(port), sleep)


# ezo-driver translation, kept as plain functions so the tests can check them
# against the installed ezo-driver without hardware.


def outcome_for(status: Any, statuses: Any) -> Outcome | None:
    """The outcome for a non-success ``DeviceStatus``, or None if unknown."""
    return {
        statuses.NOT_READY: Outcome.NOT_READY,
        statuses.FAIL: Outcome.FAIL,
        statuses.NO_DATA: Outcome.NO_DATA,
    }.get(status)


def values_from_reading(family: str, reading: Any, enums: Any) -> tuple[Value, ...]:
    """Turn an ezo-driver reading into values. Multi-output families report
    only the outputs present in the reading's mask."""
    if family == "ph":
        return (Value("", float(reading.ph), "pH"),)
    if family == "orp":
        return (Value("", float(reading.millivolts), "mV"),)
    if family == "rtd":
        if reading.temperature <= RTD_NO_PROBE:
            raise EzoDeviceError("no RTD probe connected")
        return (Value("", to_celsius(reading.temperature, reading.scale.name), "°C"),)
    mask = int(reading.present_mask)
    fields: list[tuple[Any, str, float, str]]
    if family == "ec":
        m = enums.ECOutputMask
        fields = [
            (m.CONDUCTIVITY, "conductivity", reading.conductivity_us_cm, "µS/cm"),
            (m.TOTAL_DISSOLVED_SOLIDS, "tds", reading.total_dissolved_solids_ppm, "ppm"),
            (m.SALINITY, "salinity", reading.salinity_ppt, "ppt"),
            (m.SPECIFIC_GRAVITY, "specific_gravity", reading.specific_gravity, ""),
        ]
    elif family == "do":
        m = enums.DOOutputMask
        fields = [
            (m.MG_L, "mg_l", reading.milligrams_per_liter, "mg/L"),
            (m.PERCENT_SATURATION, "saturation", reading.percent_saturation, "%"),
        ]
    elif family == "hum":
        m = enums.HUMOutputMask
        fields = [
            (m.HUMIDITY, "humidity", reading.relative_humidity_percent, "%"),
            (m.AIR_TEMPERATURE, "air_temperature", reading.air_temperature_c, "°C"),
            (m.DEW_POINT, "dew_point", reading.dew_point_c, "°C"),
        ]
    else:
        raise ValueError(f"unknown EZO family {family!r}")
    return tuple(Value(f, float(v), u) for bit, f, v, u in fields if mask & int(bit))


# Calibration status by family, from each datasheet's "Cal,?" reply.
_LEVELS = {
    "ph": {0: "not calibrated", 1: "one point", 2: "two point", 3: "three point"},
    "ec": {0: "not calibrated", 1: "two point", 2: "three point"},
    "do": {0: "not calibrated", 1: "one point", 2: "two point"},
}


def calibration_text(family: str, status: Any) -> str:
    if family == "hum":
        return "temperature calibrated" if status.calibrated else "temperature not calibrated"
    if family in ("orp", "rtd"):
        return "calibrated" if status.calibrated else "not calibrated"
    level = int(status.level)
    return _LEVELS[family].get(level, f"calibration level {level}")


def calibration_args(family: str, point: str, value: float | None, enums: Any) -> tuple[Any, ...]:
    """The arguments after the device for ezo-driver's calibration call."""
    if family == "ph":
        return (enums.PHCalibrationPoint[point.upper()], value)
    if family in ("orp", "rtd", "hum"):
        return (value,)
    if family == "ec":
        points = {
            "dry": enums.ECCalibrationPoint.DRY,
            "single": enums.ECCalibrationPoint.SINGLE_POINT,
            "low": enums.ECCalibrationPoint.LOW_POINT,
            "high": enums.ECCalibrationPoint.HIGH_POINT,
        }
        # The dry point sends "Cal,dry"; its reference value is ignored.
        return (points[point], value if value is not None else 0.0)
    if family == "do":
        return (enums.DOCalibrationPoint[point.upper()],)
    raise ValueError(f"unknown EZO family {family!r}")


def to_celsius(value: float, scale: str) -> float:
    if scale == "KELVIN":
        return value - 273.15
    if scale == "FAHRENHEIT":
        return (value - 32) * 5 / 9
    return value


class DriverPort:
    """``EzoPort`` over ezo-driver, on a Linux I2C bus."""

    def __init__(self, kind: str, bus: str, address: int):
        import ezo_driver
        from ezo_driver import control, do, ec, hum, orp, ph, rtd

        self._ezo = ezo_driver
        self._control = control
        self._family = FAMILIES[kind]
        self._module: Any = {"ph": ph, "orp": orp, "rtd": rtd, "ec": ec, "do": do, "hum": hum}[
            self._family.name
        ]
        self._product = getattr(ezo_driver.ProductId, self._family.name.upper())
        try:
            self._dev = ezo_driver.LinuxI2CDevice(bus_number(bus), address)
        except ezo_driver.EzoError as e:
            raise EzoDeviceError(f"cannot open {bus} at 0x{address:02X}: {e}") from e

    def _call(self, func: Callable[..., Any], *args: Any) -> Any:
        try:
            return func(self._dev, *args)
        except self._ezo.EzoProtocolError as e:
            outcome = outcome_for(self._dev.last_status, self._ezo.DeviceStatus)
            if outcome is not None:
                raise EzoStatusError(outcome) from e
            raise EzoDeviceError(str(e)) from e
        except self._ezo.EzoError as e:
            raise EzoDeviceError(str(e)) from e

    def send_info_query(self) -> int:
        return self._call(self._control.send_info_query_i2c, self._product)

    def read_family(self) -> str:
        info = self._call(self._control.read_info_i2c)
        if info.product_id == self._ezo.ProductId.UNKNOWN:
            return f"unknown ({info.product_code})"
        return info.product_id.name.lower()

    def send_config_query(self) -> int:
        name = self._family.name
        if name in ("ec", "do", "hum"):
            return self._call(self._module.send_output_query_i2c)
        if name == "rtd":
            return self._call(self._module.send_scale_query_i2c)
        return 0

    def read_config(self) -> int | None:
        name = self._family.name
        if name in ("ec", "do", "hum"):
            return int(self._call(self._module.read_output_config_i2c).enabled_mask)
        if name == "rtd":
            return int(self._call(self._module.read_scale_i2c).scale)
        return None

    def send_read(self, temperature_c: float | None) -> int:
        if temperature_c is not None and self._family.temp_comp:
            return self._call(self._module.send_read_with_temp_comp_i2c, temperature_c)
        return self._call(self._module.send_read_i2c)

    def read_values(self, config: int | None) -> tuple[Value, ...]:
        if self._family.name in ("ph", "orp"):
            reading = self._call(self._module.read_response_i2c)
        else:
            reading = self._call(self._module.read_response_i2c, config or 0)
        return values_from_reading(self._family.name, reading, self._ezo.enums)

    def send_temperature(self, temperature_c: float) -> int:
        return self._call(self._module.send_temperature_set_i2c, temperature_c)

    def send_calibration_query(self) -> int:
        if self._family.name == "hum":
            return self._call(self._module.send_temperature_calibration_query_i2c)
        return self._call(self._module.send_calibration_query_i2c)

    def read_calibration_status(self) -> str:
        if self._family.name == "hum":
            status = self._call(self._module.read_temperature_calibration_status_i2c)
        else:
            status = self._call(self._module.read_calibration_status_i2c)
        return calibration_text(self._family.name, status)

    def send_calibration_clear(self) -> int:
        if self._family.name == "hum":
            return self._call(self._module.send_clear_temperature_calibration_i2c)
        return self._call(self._module.send_clear_calibration_i2c)

    def send_calibration(self, point: str, value: float | None) -> int:
        name = self._family.name
        func = (
            self._module.send_temperature_calibration_i2c
            if name == "hum"
            else self._module.send_calibration_i2c
        )
        return self._call(func, *calibration_args(name, point, value, self._ezo.enums))

    def read_ack(self) -> None:
        status, _ = self._call(lambda dev: dev.read_response_raw())
        if status == self._ezo.DeviceStatus.SUCCESS:
            return
        outcome = outcome_for(status, self._ezo.DeviceStatus)
        if outcome is None:
            raise EzoDeviceError(f"unexpected status {status!r}")
        raise EzoStatusError(outcome)

    def close(self) -> None:
        self._dev.close()
