"""Atlas Scientific EZO circuits: identity check, split-phase reads with
temperature compensation, and calibration.

Each circuit is reached through an ``EzoPort``. ``DriverPort`` implements it
over ezo-driver on Linux I2C; tests use a fake. ``EzoReader`` sends every read,
then reads each circuit when its own delay is up, so one slow circuit never
holds up another.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

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


FAMILIES: dict[str, Family] = {
    "ezo-ph": Family("ph", True, {"mid": True, "low": True, "high": True}),
    "ezo-orp": Family("orp", False, {"ref": True}),
    "ezo-rtd": Family("rtd", False, {"ref": True}),
    "ezo-ec": Family("ec", True, {"dry": False, "single": True, "low": True, "high": True}),
    "ezo-do": Family("do", True, {"atmospheric": False, "zero": False}),
    "ezo-hum": Family("hum", False, {"temperature": True}),
}


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
    temperature_c: float | None = None


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

    ``begin`` sends a read to every circuit that has none outstanding and
    ``collect`` reads the ones whose delay is up. pH, EC and DO compensate with
    the last temperature read from the RTD named in their ``temp_comp``.
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

    def _wait_then(self, wait_ms: int) -> None:
        self._sleep(wait_ms / 1000)

    def prepare(self) -> list[Result]:
        """Check each circuit's type against the config and cache the output
        masks of DO, EC and HUM and the RTD scale. A circuit that fails is
        disabled. Blocks for each query's delay."""
        problems: list[Result] = []
        for ch in self.channels:
            try:
                self._wait_then(ch.port.send_info_query())
                family = ch.port.read_family()
                if family != ch.family.name:
                    raise EzoDeviceError(
                        f"config says {ch.device.kind}, the circuit reports {family!r}"
                    )
                self._wait_then(ch.port.send_config_query())
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

    def begin(self, names: Iterable[str] | None = None) -> list[Result]:
        """Start a cycle. Returns the circuits whose read could not be sent."""
        now = self._clock()
        wanted = set(names) if names is not None else None
        failed: list[Result] = []
        for ch in self.channels:
            if not ch.enabled or (wanted is not None and ch.name not in wanted):
                continue
            pending = self._pending.get(ch.name)
            if pending is not None:
                # Retried this cycle without sending a new read.
                pending.waiting_for_cycle = False
                pending.due = now
                continue
            temperature = self._temperature_for(ch)
            try:
                wait_ms = ch.port.send_read(temperature)
            except (EzoStatusError, EzoDeviceError) as e:
                failed.append(Result(ch.name, Outcome.ERROR, detail=str(e)))
                continue
            self._pending[ch.name] = _Pending(now + wait_ms / 1000, temperature)
        return failed

    def next_due(self) -> float | None:
        dues = [p.due for p in self._pending.values() if not p.waiting_for_cycle]
        return min(dues) if dues else None

    def collect(self) -> list[Result]:
        """Read every circuit whose delay is up."""
        now = self._clock()
        results: list[Result] = []
        for name, pending in list(self._pending.items()):
            if pending.waiting_for_cycle or pending.due > now:
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
                results.append(Result(name, e.outcome, temperature_c=pending.temperature_c))
                continue
            except EzoDeviceError as e:
                del self._pending[name]
                results.append(Result(name, Outcome.ERROR, detail=str(e)))
                continue
            del self._pending[name]
            if ch.family.name == "rtd":
                celsius = _celsius(values)
                if celsius is not None:
                    self._temperatures[name] = celsius
            results.append(Result(name, Outcome.OK, values, temperature_c=pending.temperature_c))
        return results

    def run_cycle(self, names: Iterable[str] | None = None) -> list[Result]:
        """Blocking cycle for the CLI: send, wait for each delay, read, and
        give a NOT_READY circuit one more cycle."""
        names = list(names) if names is not None else None
        results = self.begin(names)
        for _ in range(2):
            while (due := self.next_due()) is not None:
                self._sleep(max(0.0, due - self._clock()))
                results += self.collect()
            if not self._pending:
                break
            results += self.begin(names)
        return results

    def read_once(self) -> list[Result]:
        """Read every circuit once. RTDs used for compensation are read first,
        so pH, EC and DO compensate with this reading's temperature."""
        sources = {
            ch.device.temp_comp
            for ch in self.channels
            if ch.enabled and ch.family.temp_comp and ch.device.temp_comp
        }
        first = self.run_cycle(sources) if sources else []
        rest = self.run_cycle(n for n in self._by_name if n not in sources)
        order = {name: i for i, name in enumerate(self._by_name)}
        return sorted(first + rest, key=lambda r: order[r.channel])


def run_command(port: EzoPort, send: Callable[[], int], sleep: Callable[[float], None]) -> None:
    """Send a command, wait its delay, and check the circuit accepted it."""
    sleep(send() / 1000)
    port.read_ack()


def calibration_status(port: EzoPort, sleep: Callable[[float], None] = time.sleep) -> str:
    sleep(port.send_calibration_query() / 1000)
    return port.read_calibration_status()


def calibrate(
    family: Family,
    port: EzoPort,
    point: str,
    value: float | None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
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
    run_command(port, lambda: port.send_calibration(point, value), sleep)


def clear_calibration(port: EzoPort, sleep: Callable[[float], None] = time.sleep) -> None:
    run_command(port, port.send_calibration_clear, sleep)


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
        status_type = self._ezo.DeviceStatus
        try:
            return func(self._dev, *args)
        except self._ezo.EzoProtocolError as e:
            status = self._dev.last_status
            outcome = {
                status_type.NOT_READY: Outcome.NOT_READY,
                status_type.FAIL: Outcome.FAIL,
                status_type.NO_DATA: Outcome.NO_DATA,
            }.get(status)
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
        name = self._family.name
        if name == "ph":
            return (Value("", self._call(self._module.read_response_i2c).ph, "pH"),)
        if name == "orp":
            return (Value("", self._call(self._module.read_response_i2c).millivolts, "mV"),)
        if name == "rtd":
            r = self._call(self._module.read_response_i2c, config or 0)
            return (Value("", _to_celsius(r.temperature, r.scale.name), "°C"),)
        r = self._call(self._module.read_response_i2c, config or 0)
        mask = int(r.present_mask)
        enums = self._ezo.enums
        fields: list[tuple[int, str, float, str]]
        if name == "ec":
            m = enums.ECOutputMask
            fields = [
                (m.CONDUCTIVITY, "conductivity", r.conductivity_us_cm, "µS/cm"),
                (m.TOTAL_DISSOLVED_SOLIDS, "tds", r.total_dissolved_solids_ppm, "ppm"),
                (m.SALINITY, "salinity", r.salinity_ppt, "ppt"),
                (m.SPECIFIC_GRAVITY, "specific_gravity", r.specific_gravity, ""),
            ]
        elif name == "do":
            m = enums.DOOutputMask
            fields = [
                (m.MG_L, "mg_l", r.milligrams_per_liter, "mg/L"),
                (m.PERCENT_SATURATION, "saturation", r.percent_saturation, "%"),
            ]
        else:
            m = enums.HUMOutputMask
            fields = [
                (m.HUMIDITY, "humidity", r.relative_humidity_percent, "%"),
                (m.AIR_TEMPERATURE, "air_temperature", r.air_temperature_c, "°C"),
                (m.DEW_POINT, "dew_point", r.dew_point_c, "°C"),
            ]
        return tuple(Value(f, v, u) for bit, f, v, u in fields if mask & int(bit))

    def send_calibration_query(self) -> int:
        if self._family.name == "hum":
            return self._call(self._module.send_temperature_calibration_query_i2c)
        return self._call(self._module.send_calibration_query_i2c)

    def read_calibration_status(self) -> str:
        name = self._family.name
        if name == "hum":
            s = self._call(self._module.read_temperature_calibration_status_i2c)
            return "temperature calibrated" if s.calibrated else "temperature not calibrated"
        s = self._call(self._module.read_calibration_status_i2c)
        if name == "ph":
            return s.level.name.lower().replace("_", " ")
        if name in ("orp", "rtd"):
            return "calibrated" if s.calibrated else "not calibrated"
        return f"{s.level} point(s)" if s.level else "not calibrated"

    def send_calibration_clear(self) -> int:
        if self._family.name == "hum":
            return self._call(self._module.send_clear_temperature_calibration_i2c)
        return self._call(self._module.send_clear_calibration_i2c)

    def send_calibration(self, point: str, value: float | None) -> int:
        enums = self._ezo.enums
        name = self._family.name
        if name == "ph":
            return self._call(
                self._module.send_calibration_i2c,
                enums.PHCalibrationPoint[point.upper()],
                value,
            )
        if name in ("orp", "rtd"):
            return self._call(self._module.send_calibration_i2c, value)
        if name == "ec":
            points = {
                "dry": enums.ECCalibrationPoint.DRY,
                "single": enums.ECCalibrationPoint.SINGLE_POINT,
                "low": enums.ECCalibrationPoint.LOW_POINT,
                "high": enums.ECCalibrationPoint.HIGH_POINT,
            }
            # The dry point sends "Cal,dry"; its reference value is ignored.
            return self._call(self._module.send_calibration_i2c, points[point], value or 0.0)
        if name == "do":
            return self._call(
                self._module.send_calibration_i2c, enums.DOCalibrationPoint[point.upper()]
            )
        return self._call(self._module.send_temperature_calibration_i2c, value)

    def read_ack(self) -> None:
        status, _ = self._dev.read_response_raw()
        statuses = self._ezo.DeviceStatus
        if status != statuses.SUCCESS:
            outcome = {
                statuses.NOT_READY: Outcome.NOT_READY,
                statuses.FAIL: Outcome.FAIL,
                statuses.NO_DATA: Outcome.NO_DATA,
            }.get(status, Outcome.ERROR)
            raise EzoStatusError(outcome)

    def close(self) -> None:
        self._dev.close()


def _to_celsius(value: float, scale: str) -> float:
    if scale == "KELVIN":
        return value - 273.15
    if scale == "FAHRENHEIT":
        return (value - 32) * 5 / 9
    return value
