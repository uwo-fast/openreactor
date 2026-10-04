"""The openreactor TOML configuration: one file for the server, the controller
timing and each device. Nothing is discovered; every device is listed here."""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    # Holds the controller lock and, later, the database. None means the
    # per-user default; see openreactor.lock.state_dir().
    state_dir: str | None = None


@dataclass(frozen=True)
class ChannelConfig:
    """An actuated output on a slice: an RLHT heater or a DCMT motor."""

    name: str
    label: str
    output: int
    tc: int | None = None


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
        self.unknown_keys(data, ("server", "controller", "device"), "")
        server = self.server(data.get("server", {}))
        controller = self.controller(data.get("controller", {}))

        raw_devices = data.get("device", [])
        if not isinstance(raw_devices, list):
            self.error("device", "must be an array of tables ([[device]])")
            raw_devices = []
        indexed = [
            (f"device[{i}]", self.device(raw, f"device[{i}]")) for i, raw in enumerate(raw_devices)
        ]
        devices = [(path, d) for path, d in indexed if d is not None]

        self.cross_checks(devices, controller)
        return Config(server=server, controller=controller, devices=tuple(d for _, d in devices))

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
        return ServerConfig(host=host, port=port, password_hash=password_hash)

    def controller(self, raw: Any) -> ControllerConfig:
        default = ControllerConfig()
        t = self.table(raw, "controller")
        if t is None:
            return default
        self.unknown_keys(
            t, ("slice_poll_s", "ezo_period_s", "watchdog_timeout_ms", "state_dir"), "controller"
        )
        slice_poll_s = default.slice_poll_s
        if "slice_poll_s" in t:
            value = self.positive(t["slice_poll_s"], "controller.slice_poll_s")
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
        state_dir = None
        if "state_dir" in t:
            state_dir = self.string(t, "state_dir", "controller")
            if state_dir is not None and not state_dir.startswith("/"):
                self.error("controller.state_dir", "must be an absolute path")
                state_dir = None
        return ControllerConfig(slice_poll_s, ezo_period_s, watchdog_timeout_ms, state_dir)

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
        allowed = ("label", out_key, "tc") if kind == "rlht" else ("label", out_key)
        result: list[ChannelConfig] = []
        used: dict[int, str] = {}
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
            if name_ok:
                result.append(ChannelConfig(name=ch_name, label=label, output=output or 0, tc=tc))
        return tuple(result)

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
