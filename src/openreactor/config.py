"""The openreactor TOML configuration: one file for the server, the controller
timing and each device. Nothing is discovered; every device is listed here."""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openreactor.auth import looks_like_hash

SLICE_KINDS = ("rlht", "dcmt")
EZO_KINDS = ("ezo-ph", "ezo-orp", "ezo-rtd", "ezo-ec", "ezo-do", "ezo-hum")
KINDS = SLICE_KINDS + EZO_KINDS

# I2C read delays in ms, plain and with temperature compensation, from
# ezo-driver's product table. Only pH, EC and DO compensate.
EZO_READ_MS = {
    "ezo-ph": 900,
    "ezo-orp": 1000,
    "ezo-rtd": 600,
    "ezo-ec": 600,
    "ezo-do": 600,
    "ezo-hum": 300,
}
EZO_TEMP_COMP_READ_MS = {"ezo-ph": 900, "ezo-ec": 900, "ezo-do": 900}
EZO_PERIOD_MARGIN_MS = 200

ADDRESS_MIN = 0x08
ADDRESS_MAX = 0x77
WATCHDOG_MAX_MS = 65535  # a u16 on the wire

# RLHT gains go to the slice as one byte each, ten times the gain.
GAINS = ("kp", "ki", "kd")
# The slice takes setpoints as i16 tenths of a degree.
SETPOINT_MAX_C = 3276.7
GAIN_MAX = 25.5
# An RLHT output's time-proportioning period: the firmware clamps anything
# else into this range without saying so.
PERIOD_MIN_MS = 100
PERIOD_MAX_MS = 10000
# A slice poll takes two controller ticks (SET_REPLY, then the read), so a
# shorter poll period would not poll any faster.
SLICE_POLL_MIN_S = 0.2

# The key that picks the hardware output for each slice channel.
OUTPUT_KEY = {"rlht": "output", "dcmt": "motor"}


def _hex(n: int) -> str:
    return f"0x{n:02X}" if n >= 0 else str(n)


_NAME = re.compile(r"[a-z][a-z0-9_]*")
# A Linux i2c-dev bus, e.g. /dev/i2c-1.
I2C_BUS = re.compile(r"/dev/i2c-(\d+)")


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    password_hash: str | None = None


@dataclass(frozen=True)
class ControllerConfig:
    slice_poll_s: float = 1.0
    ezo_period_s: float = 2.0
    watchdog_timeout_ms: int = 5000


@dataclass(frozen=True)
class StorageConfig:
    # None means the per-user default; see openreactor.storage.default_path().
    database: str | None = None


@dataclass(frozen=True)
class ChannelConfig:
    """An actuated output on a slice: an RLHT heater or a DCMT motor."""

    name: str
    label: str
    output: int
    tc: int | None = None
    # RLHT only, each optional: sent to the slice only when set.
    kp: float | None = None
    ki: float | None = None
    kd: float | None = None
    period_ms: int | None = None
    # RLHT only: the highest setpoint the API and UI accept, in °C.
    max_setpoint: float | None = None


@dataclass(frozen=True)
class DeviceConfig:
    name: str
    kind: str
    bus: str
    address: int
    channels: tuple[ChannelConfig, ...] = ()
    allow_unprotected: bool = False
    temp_comp: str | None = None


@dataclass(frozen=True)
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    controller: ControllerConfig = field(default_factory=ControllerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    devices: tuple[DeviceConfig, ...] = ()


@dataclass(frozen=True)
class Problem:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class ConfigError(Exception):
    def __init__(self, problems: list[Problem]):
        self.problems = problems
        super().__init__("\n".join(str(p) for p in problems))


def load_config(path: str | Path) -> Config:
    """Read and validate a config file. Raises ConfigError listing every problem."""
    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError([Problem(str(path), f"not valid TOML: {e}")]) from e
        except UnicodeDecodeError as e:
            raise ConfigError([Problem(str(path), f"not valid UTF-8: {e.reason}")]) from e
    return parse_config(data)


def parse_config(data: dict[str, Any]) -> Config:
    """Validate a parsed TOML document. Raises ConfigError listing every problem."""
    parser = _Parser()
    config = parser.config(data)
    if parser.problems:
        raise ConfigError(parser.problems)
    return config


class _Parser:
    def __init__(self) -> None:
        self.problems: list[Problem] = []
        # Names of devices that failed validation, so a reference to one is
        # not reported a second time as a missing device.
        self.invalid_names: set[str] = set()
        # Timing settings that failed validation; the rules between them are
        # then skipped rather than checked against a default.
        self.invalid_timing: set[str] = set()

    def error(self, path: str, message: str) -> None:
        self.problems.append(Problem(path, message))

    def table(self, value: Any, path: str) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            self.error(path, "must be a table")
            return None
        return value

    def unknown_keys(self, t: dict[str, Any], allowed: tuple[str, ...], path: str) -> None:
        for key in t:
            if key not in allowed:
                self.error(f"{path}.{key}" if path else key, "unknown key")

    def string(self, t: dict[str, Any], key: str, path: str) -> str | None:
        if key not in t:
            self.error(f"{path}.{key}", "is required")
            return None
        value = t[key]
        if not isinstance(value, str) or not value:
            self.error(f"{path}.{key}", "must be a non-empty string")
            return None
        return value

    def integer(self, value: Any, path: str) -> int | None:
        # bool is a subclass of int in Python, but true is never a number here.
        if isinstance(value, bool) or not isinstance(value, int):
            self.error(path, "must be an integer")
            return None
        return value

    def positive(self, value: Any, path: str) -> float | None:
        if isinstance(value, bool) or not isinstance(value, int | float):
            self.error(path, "must be a number")
            return None
        if not math.isfinite(value):
            self.error(path, "must be a finite number")
            return None
        if not value > 0:
            self.error(path, "must be greater than 0")
            return None
        return float(value)

    def name(self, value: str, path: str) -> bool:
        if not _NAME.fullmatch(value):
            self.error(path, "must start with a lowercase letter and use only a-z, 0-9 and _")
            return False
        return True

    def config(self, data: dict[str, Any]) -> Config:
        self.unknown_keys(data, ("server", "controller", "storage", "device"), "")
        server = self.server(data.get("server", {}))
        controller = self.controller(data.get("controller", {}))
        storage = self.storage(data.get("storage", {}))

        raw_devices = data.get("device", [])
        if not isinstance(raw_devices, list):
            self.error("device", "must be an array of tables ([[device]])")
            raw_devices = []
        indexed = [
            (f"device[{i}]", self.device(raw, f"device[{i}]")) for i, raw in enumerate(raw_devices)
        ]
        devices = [(path, d) for path, d in indexed if d is not None]

        self.cross_checks(devices, controller)
        return Config(
            server=server,
            controller=controller,
            storage=storage,
            devices=tuple(d for _, d in devices),
        )

    def storage(self, raw: Any) -> StorageConfig:
        t = self.table(raw, "storage")
        if t is None:
            return StorageConfig()
        self.unknown_keys(t, ("database",), "storage")
        database = None
        if "database" in t:
            database = self.string(t, "database", "storage")
            if database is not None and not database.startswith("/"):
                self.error("storage.database", "must be an absolute path")
                database = None
        return StorageConfig(database)

    def server(self, raw: Any) -> ServerConfig:
        default = ServerConfig()
        t = self.table(raw, "server")
        if t is None:
            return default
        self.unknown_keys(t, ("host", "port", "password_hash"), "server")
        host = default.host
        if "host" in t:
            host = self.string(t, "host", "server") or host
        port = default.port
        if "port" in t:
            value = self.integer(t["port"], "server.port")
            if value is not None and not 1 <= value <= 65535:
                self.error("server.port", "must be between 1 and 65535")
            elif value is not None:
                port = value
        password_hash = None
        if "password_hash" in t:
            password_hash = self.string(t, "password_hash", "server")
            if password_hash is not None and not looks_like_hash(password_hash):
                self.error("server.password_hash", "is not a hash from openreactor hash-password")
                password_hash = None
        return ServerConfig(host=host, port=port, password_hash=password_hash)

    def controller(self, raw: Any) -> ControllerConfig:
        default = ControllerConfig()
        t = self.table(raw, "controller")
        if t is None:
            return default
        self.unknown_keys(t, ("slice_poll_s", "ezo_period_s", "watchdog_timeout_ms"), "controller")
        slice_poll_s = default.slice_poll_s
        if "slice_poll_s" in t:
            value = self.positive(t["slice_poll_s"], "controller.slice_poll_s")
            if value is not None and value < SLICE_POLL_MIN_S:
                self.error(
                    "controller.slice_poll_s",
                    f"must be at least {SLICE_POLL_MIN_S:g} s: a poll takes two 0.1 s ticks",
                )
                value = None
            if value is None:
                self.invalid_timing.add("slice_poll_s")
            else:
                slice_poll_s = value
        ezo_period_s = default.ezo_period_s
        if "ezo_period_s" in t:
            value = self.positive(t["ezo_period_s"], "controller.ezo_period_s")
            if value is None:
                self.invalid_timing.add("ezo_period_s")
            else:
                ezo_period_s = value
        watchdog_timeout_ms = default.watchdog_timeout_ms
        if "watchdog_timeout_ms" in t:
            wd = self.integer(t["watchdog_timeout_ms"], "controller.watchdog_timeout_ms")
            if wd is None:
                self.invalid_timing.add("watchdog_timeout_ms")
            else:
                watchdog_timeout_ms = wd
        return ControllerConfig(slice_poll_s, ezo_period_s, watchdog_timeout_ms)

    def device(self, raw: Any, path: str) -> DeviceConfig | None:
        t = self.table(raw, path)
        if t is None:
            return None

        kind = self.string(t, "kind", path)
        if kind is not None and kind not in KINDS:
            self.error(f"{path}.kind", f"unknown kind {kind!r}; expected one of {', '.join(KINDS)}")
            kind = None
        if kind is not None:
            allowed = ["name", "kind", "bus", "address"]
            if kind in SLICE_KINDS:
                allowed += ["channels", "allow_unprotected"]
            if kind in EZO_TEMP_COMP_READ_MS:
                allowed.append("temp_comp")
            self.unknown_keys(t, tuple(allowed), path)

        name = self.string(t, "name", path)
        if name is not None and not self.name(name, f"{path}.name"):
            name = None
        bus = self.string(t, "bus", path)
        if bus is not None and not I2C_BUS.fullmatch(bus):
            self.error(f"{path}.bus", f"{bus!r} is not a Linux I2C bus such as /dev/i2c-1")
            bus = None

        address = None
        if "address" not in t:
            self.error(f"{path}.address", "is required")
        else:
            address = self.integer(t["address"], f"{path}.address")
            if address is not None and not ADDRESS_MIN <= address <= ADDRESS_MAX:
                self.error(
                    f"{path}.address",
                    f"{_hex(address)} is outside the device range "
                    f"0x{ADDRESS_MIN:02X} to 0x{ADDRESS_MAX:02X}",
                )

        allow_unprotected = t.get("allow_unprotected", False)
        if kind in SLICE_KINDS and not isinstance(allow_unprotected, bool):
            self.error(f"{path}.allow_unprotected", "must be true or false")

        temp_comp = None
        if kind in EZO_TEMP_COMP_READ_MS and "temp_comp" in t:
            temp_comp = self.string(t, "temp_comp", path)

        channels: tuple[ChannelConfig, ...] = ()
        if kind in SLICE_KINDS:
            assert kind is not None
            if "channels" not in t:
                self.error(f"{path}.channels", "is required")
            else:
                channels = self.channels(kind, t["channels"], f"{path}.channels")

        # A device whose identity parsed stays in the cross-device checks even
        # when another field failed, so every problem is reported in one pass.
        if name is None or kind is None or bus is None or address is None:
            if name is not None:
                self.invalid_names.add(name)
            return None
        if not isinstance(allow_unprotected, bool):
            allow_unprotected = False
        return DeviceConfig(
            name=name,
            kind=kind,
            bus=bus,
            address=address,
            channels=channels,
            allow_unprotected=allow_unprotected,
            temp_comp=temp_comp,
        )

    def channels(self, kind: str, raw: Any, path: str) -> tuple[ChannelConfig, ...]:
        t = self.table(raw, path)
        if t is None:
            return ()
        if not t:
            self.error(path, "must define at least one channel")
            return ()
        out_key = OUTPUT_KEY[kind]
        allowed = (
            ("label", out_key, "tc", *GAINS, "period_ms", "max_setpoint")
            if kind == "rlht"
            else ("label", out_key)
        )
        result: list[ChannelConfig] = []
        used: dict[int, str] = {}
        tuned: list[str] = []
        for ch_name, raw_ch in t.items():
            cpath = f"{path}.{ch_name}"
            ch = self.table(raw_ch, cpath)
            if ch is None:
                continue
            name_ok = self.name(ch_name, cpath)
            self.unknown_keys(ch, allowed, cpath)
            label = ch.get("label", ch_name)
            if not isinstance(label, str) or not label:
                self.error(f"{cpath}.label", "must be a non-empty string")
                label = ch_name
            output = self.one_or_two(ch, out_key, cpath)
            tc = self.one_or_two(ch, "tc", cpath) if kind == "rlht" else None
            if output is not None and output in used:
                self.error(
                    f"{cpath}.{out_key}", f"{out_key} {output} is already used by {used[output]!r}"
                )
            if output is not None:
                used.setdefault(output, ch_name)
            # Kept even when a field failed, so its name is still checked against
            # the other devices; any error recorded here fails the whole config.
            gains = self.gains(ch, cpath) if kind == "rlht" else None
            period_ms = None
            if kind == "rlht" and "period_ms" in ch:
                period_ms = self.integer(ch["period_ms"], f"{cpath}.period_ms")
                if period_ms is not None and not PERIOD_MIN_MS <= period_ms <= PERIOD_MAX_MS:
                    self.error(
                        f"{cpath}.period_ms",
                        f"must be between {PERIOD_MIN_MS} and {PERIOD_MAX_MS}, the range the "
                        "slice uses",
                    )
                    period_ms = None
            max_setpoint = None
            if kind == "rlht" and "max_setpoint" in ch:
                max_setpoint = self.positive(ch["max_setpoint"], f"{cpath}.max_setpoint")
                if max_setpoint is not None and max_setpoint > SETPOINT_MAX_C:
                    self.error(
                        f"{cpath}.max_setpoint",
                        f"must be at most {SETPOINT_MAX_C}, the highest setpoint the slice takes",
                    )
                    max_setpoint = None
            if gains is not None:
                tuned.append(ch_name)
            if name_ok:
                kp, ki, kd = gains or (None, None, None)
                result.append(
                    ChannelConfig(
                        name=ch_name,
                        label=label,
                        output=output or 0,
                        tc=tc,
                        kp=kp,
                        ki=ki,
                        kd=kd,
                        period_ms=period_ms,
                        max_setpoint=max_setpoint,
                    )
                )
        if kind == "rlht" and tuned and (len(tuned) != len(t) or set(used) != {1, 2}):
            # The slice takes both outputs' gains in one command, and they
            # cannot be read back: gains go only when the config gives all six.
            self.error(
                path,
                "gains (kp, ki, kd) must be set on a channel for each of the slice's two "
                "outputs, or on none: the slice sets both outputs' gains at once",
            )
        return tuple(result)

    def gains(self, ch: dict[str, Any], path: str) -> tuple[float, float, float] | None:
        """kp, ki and kd: all three or none. The slice takes each as a byte
        holding ten times the gain, so 0 to 25.5 in steps of 0.1."""
        given = [g for g in GAINS if g in ch]
        if not given:
            return None
        if len(given) != len(GAINS):
            missing = ", ".join(g for g in GAINS if g not in ch)
            self.error(
                path, f"sets {', '.join(given)} without {missing}: set kp, ki and kd together"
            )
            return None
        values: list[float] = []
        for g in GAINS:
            value = ch[g]
            if isinstance(value, bool) or not isinstance(value, int | float):
                self.error(f"{path}.{g}", "must be a number")
                return None
            if not (math.isfinite(value) and 0 <= value <= GAIN_MAX):
                self.error(f"{path}.{g}", f"must be between 0 and {GAIN_MAX}")
                return None
            if abs(value * 10 - round(value * 10)) > 1e-9:
                self.error(f"{path}.{g}", "must be a multiple of 0.1, as the slice stores it")
                return None
            if g == "ki" and value == 0:
                # The RLHT firmware's PID freezes its integral when ki is 0
                # and does not clear it on the safe state, so a heater could
                # stay at its last duty.
                self.error(
                    f"{path}.ki",
                    "must be above 0: with ki = 0 the slice's PID keeps its integral, "
                    "and a heater can stay on after its setpoint goes to 0",
                )
                return None
            values.append(float(value))
        return values[0], values[1], values[2]

    def one_or_two(self, t: dict[str, Any], key: str, path: str) -> int | None:
        if key not in t:
            self.error(f"{path}.{key}", "is required (1 or 2)")
            return None
        value = self.integer(t[key], f"{path}.{key}")
        if value is not None and value not in (1, 2):
            self.error(f"{path}.{key}", "must be 1 or 2")
            return None
        return value

    def cross_checks(
        self, devices: list[tuple[str, DeviceConfig]], controller: ControllerConfig
    ) -> None:
        by_name: dict[str, DeviceConfig] = {}
        by_bus_address: dict[tuple[str, int], str] = {}
        # Device and channel names share one namespace: an EZO device is
        # itself a channel, and both will name things in the UI and the API.
        owner: dict[str, str] = {}

        for path, d in devices:
            if d.name in by_name:
                self.error(f"{path}.name", f"name {d.name!r} is already used by {owner[d.name]}")
                continue
            by_name[d.name] = d
            if d.name in owner:
                self.error(f"{path}.name", f"name {d.name!r} is already used by {owner[d.name]}")
            owner.setdefault(d.name, f"device {d.name!r}")

            if (d.bus, d.address) in by_bus_address:
                other = by_bus_address[(d.bus, d.address)]
                self.error(
                    f"{path}.address", f"0x{d.address:02X} on {d.bus} is already used by {other!r}"
                )
            by_bus_address.setdefault((d.bus, d.address), d.name)

            for ch in d.channels:
                if ch.name in owner:
                    self.error(
                        f"{path}.channels.{ch.name}",
                        f"name {ch.name!r} is already used by {owner[ch.name]}",
                    )
                owner.setdefault(ch.name, f"channel {ch.name!r} of {d.name!r}")

        for path, d in devices:
            if d.temp_comp is None or d.temp_comp in self.invalid_names:
                continue
            target = by_name.get(d.temp_comp)
            if target is None:
                self.error(f"{path}.temp_comp", f"{d.temp_comp!r} is not a device")
            elif target.kind != "ezo-rtd":
                self.error(
                    f"{path}.temp_comp",
                    f"must name an ezo-rtd device; {d.temp_comp!r} is {target.kind}",
                )

        # Round so 0.1 s x 3 is exactly 300 ms. A huge poll overflows to inf,
        # so compare with the limit before rounding up to whole milliseconds.
        three_polls_ms = round(3 * controller.slice_poll_s * 1000, 6)
        timing_ok = not self.invalid_timing & {"slice_poll_s", "watchdog_timeout_ms"}
        if timing_ok and three_polls_ms > WATCHDOG_MAX_MS:
            self.error(
                "controller.slice_poll_s",
                f"must be at most {WATCHDOG_MAX_MS / 3000:g} s, so that 3 polls fit in "
                f"the longest watchdog timeout, {WATCHDOG_MAX_MS} ms",
            )
        elif timing_ok and not (
            math.ceil(three_polls_ms) <= controller.watchdog_timeout_ms <= WATCHDOG_MAX_MS
        ):
            self.error(
                "controller.watchdog_timeout_ms",
                f"must be between 3 x slice_poll_s ({math.ceil(three_polls_ms)} ms) "
                f"and {WATCHDOG_MAX_MS} ms",
            )

        ezo_reads = [
            (EZO_TEMP_COMP_READ_MS[d.kind] if d.temp_comp else EZO_READ_MS[d.kind], d.name)
            for _, d in devices
            if d.kind in EZO_KINDS
        ]
        if ezo_reads and "ezo_period_s" not in self.invalid_timing:
            slowest_ms, slowest = max(ezo_reads)
            min_period_ms = slowest_ms + EZO_PERIOD_MARGIN_MS
            if round(controller.ezo_period_s * 1000, 6) < min_period_ms:
                self.error(
                    "controller.ezo_period_s",
                    f"must be at least {min_period_ms / 1000:g} s: a read from {slowest!r} "
                    f"takes {slowest_ms} ms, plus {EZO_PERIOD_MARGIN_MS} ms",
                )
