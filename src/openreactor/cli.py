from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence

from openreactor import __version__
from openreactor.config import Config, ConfigError, DeviceConfig, load_config
from openreactor.ezo import (
    FAMILIES,
    DriverPort,
    EzoChannel,
    EzoDeviceError,
    EzoPort,
    EzoReader,
    EzoStatusError,
    Outcome,
    Result,
    calibrate,
    calibration_status,
    clear_calibration,
)

DEFAULT_CONFIG = "/etc/openreactor/openreactor.toml"

# The time source for reads. Tests replace both with a fake clock.
clock: Callable[[], float] = time.monotonic
sleep: Callable[[float], None] = time.sleep


def open_port(device: DeviceConfig) -> EzoPort:
    """Open one EZO circuit. Tests replace this with a fake."""
    return DriverPort(device.kind, device.bus, device.address)


def _load(path: str) -> Config | None:
    try:
        return load_config(path)
    except OSError as e:
        print(f"error: cannot read {path}: {e.strerror}", file=sys.stderr)
    except ConfigError as e:
        for problem in e.problems:
            print(f"error: {problem}", file=sys.stderr)
        print(f"{path}: {len(e.problems)} problem(s)", file=sys.stderr)
    return None


def check_config(args: argparse.Namespace) -> int:
    config = _load(args.config)
    if config is None:
        return 1
    channels = sum(max(len(d.channels), 1) for d in config.devices)
    print(f"{args.config}: OK, {len(config.devices)} device(s), {channels} channel(s)")
    return 0


def _print_results(results: Sequence[Result]) -> None:
    for r in results:
        if r.outcome is not Outcome.OK:
            detail = f": {r.detail}" if r.detail else ""
            print(f"{r.channel:<24} {r.outcome.value}{detail}")
            continue
        for v in r.values:
            label = f"{r.channel}.{v.field}" if v.field else r.channel
            line = f"{label:<24} {v.value:>10.3f} {v.unit}".rstrip()
            if r.temperature_c is not None:
                line += f"  (compensated at {r.temperature_c:.2f} °C)"
            elif r.compensation_missing:
                line += "  (no RTD temperature: the circuit used the last one it was given)"
            print(line)


def _open_channels(devices: Sequence[DeviceConfig]) -> tuple[list[EzoChannel], list[Result]]:
    channels: list[EzoChannel] = []
    problems: list[Result] = []
    for d in devices:
        try:
            channels.append(EzoChannel(d, open_port(d)))
        except EzoDeviceError as e:
            problems.append(Result(d.name, Outcome.ERROR, detail=str(e)))
    return channels, problems


def read(args: argparse.Namespace) -> int:
    config = _load(args.config)
    if config is None:
        return 1
    devices = [d for d in config.devices if d.kind in FAMILIES]
    skipped = [d.name for d in config.devices if d.kind not in FAMILIES]
    if not devices:
        print(f"error: {args.config} lists no EZO devices", file=sys.stderr)
        return 1
    if skipped:
        print(f"note: not reading {', '.join(skipped)}: slices are not read yet", file=sys.stderr)

    channels, problems = _open_channels(devices)
    try:
        reader = EzoReader(channels, clock=clock, sleep=sleep)
        problems += reader.prepare()
        _print_results(problems)
        if args.once:
            results = reader.read_once()
            _print_results(results)
            failed = problems or any(r.outcome is not Outcome.OK for r in results)
            return 1 if failed else 0
        period = config.controller.ezo_period_s
        started = clock()
        results = reader.read_once()
        while True:
            _print_results(results)
            print(flush=True)
            sleep(max(0.0, started + period - clock()))
            started = clock()
            results = reader.run_cycle()
    except KeyboardInterrupt:
        return 0
    finally:
        for ch in channels:
            ch.port.close()


def ezo_cal(args: argparse.Namespace) -> int:
    config = _load(args.config)
    if config is None:
        return 1
    device = next((d for d in config.devices if d.name == args.device), None)
    if device is None or device.kind not in FAMILIES:
        print(f"error: {args.device!r} is not an EZO device in {args.config}", file=sys.stderr)
        return 1
    family = FAMILIES[device.kind]
    action = args.action
    if action in ("status", "clear") and args.value is not None:
        print(f"error: {action} takes no value", file=sys.stderr)
        return 2
    if action == "clear" and not args.yes:
        print(
            f"error: clearing erases {device.name}'s calibration; repeat with --yes",
            file=sys.stderr,
        )
        return 2

    try:
        port = open_port(device)
    except EzoDeviceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        # Calibrating the wrong circuit would corrupt it, so check its type first.
        problems = EzoReader([EzoChannel(device, port)], clock=clock, sleep=sleep).prepare()
        if problems:
            _print_results(problems)
            return 1
        if action == "clear":
            clear_calibration(port, sleep)
        elif action != "status":
            calibrate(family, port, action, args.value, sleep)
            if family.name == "ph" and action == "mid":
                print(
                    "note: a mid-point calibration clears the low and high points; "
                    "calibrate those after it",
                    file=sys.stderr,
                )
        print(f"{device.name}: {calibration_status(port, sleep)}")
        return 0
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except EzoStatusError as e:
        print(f"error: {device.name} reported {e.outcome.value}", file=sys.stderr)
        return 1
    except EzoDeviceError as e:
        print(f"error: {device.name}: {e}", file=sys.stderr)
        return 1
    finally:
        port.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openreactor", description="Supervise a benchtop lab reactor."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    check = commands.add_parser("check-config", help="validate a config file and exit")
    check.add_argument("config", help="path to the TOML config file")
    check.set_defaults(func=check_config)

    config_help = f"path to the TOML config file (default {DEFAULT_CONFIG})"

    reading = commands.add_parser("read", help="read the EZO sensors")
    reading.add_argument("-c", "--config", default=DEFAULT_CONFIG, help=config_help)
    reading.add_argument("--once", action="store_true", help="read once and exit")
    reading.set_defaults(func=read)

    ezo = commands.add_parser("ezo", help="EZO circuit tools")
    ezo_commands = ezo.add_subparsers(dest="ezo_command", required=True, metavar="COMMAND")
    cal = ezo_commands.add_parser("cal", help="show, clear or set a circuit's calibration")
    cal.add_argument("-c", "--config", default=DEFAULT_CONFIG, help=config_help)
    cal.add_argument("device", help="the device name from the config")
    cal.add_argument(
        "action",
        help="status, clear, or a calibration point: "
        + "; ".join(f"{f.name}: {', '.join(f.points)}" for f in FAMILIES.values()),
    )
    cal.add_argument(
        "value", nargs="?", type=float, help="the reference value, if the point takes one"
    )
    cal.add_argument("--yes", action="store_true", help="confirm clearing the calibration")
    cal.set_defaults(func=ezo_cal)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
