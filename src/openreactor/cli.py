from __future__ import annotations

import argparse
import getpass
import ipaddress
import os
import signal
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING

from openreactor import __version__
from openreactor.auth import hash_password
from openreactor.config import Config, ConfigError, DeviceConfig, load_config
from openreactor.controller import Actuator, Controller, Event
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
    calibrate_steps,
    calibration_status_steps,
    clear_calibration_steps,
)
from openreactor.lock import LOCK_PATH, ControllerLock, LockHeld
from openreactor.profile import ProfileError, load_profile, timeline
from openreactor.profile import clock as profile_clock
from openreactor.storage import Recorder, StorageError, Store, default_path

if TYPE_CHECKING:
    from fastapi import FastAPI

    from openreactor.service import Service

DEFAULT_CONFIG = "/etc/openreactor/openreactor.toml"

# The time source for the controller. Tests replace both with a fake clock.
clock: Callable[[], float] = time.monotonic
sleep: Callable[[float], None] = time.sleep


# The machine-wide controller lock. Tests point it at a temporary file.
lock_path = LOCK_PATH


def open_port(device: DeviceConfig) -> EzoPort:
    """Open one EZO circuit. Tests replace this with a fake."""
    return DriverPort(device.kind, device.bus, device.address)


def actuators(config: Config) -> list[Actuator]:
    """The outputs stop-all makes safe. The slices add theirs in #24 and #25;
    tests replace this with fakes."""
    return []


# How long, after SIGINT or SIGTERM, requests in progress may take to
# finish. Stop-all is sent at once regardless; this bounds how long a slow
# client can keep a run from being ended and the circuits closed.
GRACEFUL_SHUTDOWN_S = 5


def run_server(app: FastAPI, host: str, port: int) -> None:
    """Serve ``app`` until SIGINT or SIGTERM. Tests replace this. One
    process: the controller and its lock live in it.

    The port is bound before the app starts, so a port in use is an error
    before any device is opened."""
    import socket

    import uvicorn

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        sock = socket.create_server((host, port), family=family)
    except OSError as e:
        raise OSError(e.errno, e.strerror, f"{host}:{port}") from None

    class Server(uvicorn.Server):
        def handle_exit(self, sig: int, frame: FrameType | None) -> None:
            # Stop-all before uvicorn waits for open connections to close.
            stop_now = getattr(app.state, "stop_now", None)
            if stop_now is not None:
                stop_now()
            super().handle_exit(sig, frame)

    shown = f"[{host}]" if ":" in host else host
    print(f"serving on http://{shown}:{port}; Ctrl-C to stop", file=sys.stderr, flush=True)
    config = uvicorn.Config(app, timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S)
    with sock:
        Server(config).run(sockets=[sock])


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


def _profile_validate(args: argparse.Namespace) -> int:
    channels = None
    if args.config is not None:
        config = _load(args.config)
        if config is None:
            return 1
        channels = {ch.name for d in config.devices for ch in d.channels}
    try:
        profile = load_profile(args.profile, channels)
    except ProfileError as e:
        for problem in e.problems:
            print(f"{args.profile}: {problem}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"error: {args.profile}: {e.strerror or e}", file=sys.stderr)
        return 1
    if args.dry_run:
        for line in timeline(profile):
            print(line)
        return 0
    print(
        f"{args.profile}: OK, {profile.name!r}, {len(profile.channels)} channel(s), "
        f"{profile_clock(profile.duration)}"
    )
    return 0


def _print(item: Result | Event) -> None:
    if isinstance(item, Event):
        where = f" {item.device}" if item.device else ""
        detail = f" ({item.details})" if item.details else ""
        print(f"{item.kind}{where}: {item.result}{detail}", flush=True)
        return
    if item.outcome is not Outcome.OK:
        detail = f": {item.detail}" if item.detail else ""
        print(f"{item.channel:<24} {item.outcome.value}{detail}", flush=True)
        return
    for v in item.values:
        label = f"{item.channel}.{v.field}" if v.field else item.channel
        line = f"{label:<24} {v.value:>10.3f} {v.unit}".rstrip()
        if item.temperature_c is not None:
            line += f"  (compensated at {item.temperature_c:.2f} °C)"
        elif item.compensation_missing:
            line += "  (no RTD temperature: the circuit used the last one it was given)"
        print(line, flush=True)


class _Stop(BaseException):
    """SIGTERM, handled like Ctrl-C: nothing on the way catches it as an
    ordinary error."""


def _on_sigterm(signum: int, frame: FrameType | None) -> None:
    raise _Stop


@contextmanager
def _controller(
    config: Config, devices: Sequence[DeviceConfig], *, auto_read: bool = False
) -> Iterator[tuple[Controller, list[Result]]]:
    """Take the lock, open the circuits, check them, and yield a controller
    with the startup problems. On the way out, even on Ctrl-C or SIGTERM,
    stop-all is sent before the circuits are closed."""
    with ControllerLock(lock_path):
        channels: list[EzoChannel] = []
        problems: list[Result] = []
        try:
            for d in devices:
                try:
                    channels.append(EzoChannel(d, open_port(d)))
                except EzoDeviceError as e:
                    problems.append(Result(d.name, Outcome.ERROR, detail=str(e)))
            reader = EzoReader(channels, clock=clock, sleep=sleep)
            problems += reader.prepare()
            controller = Controller(
                reader,
                actuators(config),
                ezo_period_s=config.controller.ezo_period_s,
                auto_read=auto_read,
                clock=clock,
                sleep=sleep,
            )
            try:
                yield controller, problems
            finally:
                controller.close()
        finally:
            for ch in channels:
                ch.port.close()


def _device_command(run: Callable[[argparse.Namespace, Config], int]):
    """Load the config, then run a device command, turning a held lock or an
    unusable state directory into an error and SIGTERM into Ctrl-C."""

    def command(args: argparse.Namespace) -> int:
        config = _load(args.config)
        if config is None:
            return 1
        previous = signal.signal(signal.SIGTERM, _on_sigterm)
        try:
            return run(args, config)
        except LockHeld as e:
            print(
                f"error: another controller holds {e.path} ({e.holder}); "
                "stop it first, or wait for it to finish",
                file=sys.stderr,
            )
            return 1
        except BrokenPipeError:
            # The reader of our output went away (read --follow | head).
            return 0
        except (StorageError, sqlite3.Error) as e:
            print(f"error: {_database(config)}: {e}", file=sys.stderr)
            return 1
        except OSError as e:
            where = f"{e.filename}: " if e.filename else ""
            print(f"error: {where}{e.strerror or e}", file=sys.stderr)
            return 1
        except (KeyboardInterrupt, _Stop):
            if getattr(args, "follow", False):
                return 0  # Ctrl-C is how --follow is meant to end
            print("interrupted", file=sys.stderr)
            return 130
        finally:
            signal.signal(signal.SIGTERM, previous)

    return command


def _follow(controller: Controller) -> None:
    """Print what the controller publishes until Ctrl-C, SIGTERM, or the
    reader of the output going away."""
    closed = False

    def show(item: Result | Event) -> None:
        nonlocal closed
        try:
            _print(item)
        except BrokenPipeError:
            # The reader went away (read --follow | head): stop, and send
            # what is left, the stop-all report, nowhere.
            closed = True
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())

    controller.subscribe(show)
    controller.run(lambda: closed)


def _ezo_devices(args: argparse.Namespace, config: Config) -> list[DeviceConfig] | None:
    devices = [d for d in config.devices if d.kind in FAMILIES]
    skipped = [d.name for d in config.devices if d.kind not in FAMILIES]
    if not devices:
        print(f"error: {args.config} lists no EZO devices", file=sys.stderr)
        return None
    if skipped:
        print(f"note: not reading {', '.join(skipped)}: slices are not read yet", file=sys.stderr)
    return devices


def _database(config: Config) -> Path:
    return Path(config.storage.database) if config.storage.database else default_path()


def _run(args: argparse.Namespace, config: Config) -> int:
    devices = _ezo_devices(args, config)
    if devices is None:
        return 1
    config_text = Path(args.config).read_text()
    run: int | None = None
    recorder: Recorder | None = None
    try:
        with _controller(config, devices, auto_read=True) as (controller, problems):
            # Only now, holding the controller lock, is no other run live: a
            # run still marked running was left by a process that died.
            store = Store(_database(config))
            try:
                for stale in store.interrupt_stale_runs():
                    print(
                        f"note: run {stale} was left running; marked interrupted", file=sys.stderr
                    )
                try:
                    run = store.start_run(args.name, config_text, args.notes)
                    names = {d.name: (d.name, d.kind) for d in devices}
                    recorder = Recorder(store, run, names)
                    for problem in problems:
                        _print(problem)
                        recorder(problem)
                    print(f"run {run} ({args.name}): recording; Ctrl-C to stop", file=sys.stderr)
                    store.recording()
                    controller.subscribe(recorder)
                    _follow(controller)
                finally:
                    try:
                        # Stop-all first, so its events are part of the run.
                        controller.close()
                    finally:
                        if run is not None:
                            _end_run(store, run, recorder)
            finally:
                store.close()
    except (KeyboardInterrupt, _Stop):
        if run is None:
            print("interrupted before the run started", file=sys.stderr)
            return 130
    if recorder is not None and recorder.failed is not None:
        return 1
    return 0


def _end_run(store: Store, run: int, recorder: Recorder | None) -> None:
    """Mark the run stopped, or interrupted if recording failed. This runs
    after stop-all, outside the tick, so it may wait for the database."""
    failed = recorder.failed if recorder is not None else None
    status = "stopped" if failed is None else "interrupted"
    store.setup()
    try:
        # The recorder may not have managed to mark it within its short wait.
        store.end_run(run, status)
    except sqlite3.Error as e:
        print(f"run {run}: could not be marked {status} ({e})", file=sys.stderr)
        return
    reason = f" ({failed})" if failed is not None else ""
    print(f"run {run}: {status}{reason}", file=sys.stderr)


def _open_for_reading(config: Config) -> Store | None:
    path = _database(config)
    try:
        return Store(path, read_only=True)
    except (StorageError, sqlite3.Error) as e:
        print(f"error: {path}: {e}", file=sys.stderr)
        return None


def _runs(args: argparse.Namespace) -> int:
    config = _load(args.config)
    if config is None:
        return 1
    store = _open_for_reading(config)
    if store is None:
        return 1
    try:
        for r in store.runs():
            ended = _when(r.ended) if r.ended else "-"
            print(f"{r.id:>4}  {r.status:<11}  {_when(r.started)}  {ended:<20}  {r.name}")
    except sqlite3.Error as e:
        print(f"error: {store.path}: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


def _export(args: argparse.Namespace) -> int:
    config = _load(args.config)
    if config is None:
        return 1
    destination = Path(args.output or f"run-{args.run}.zip")
    store = _open_for_reading(config)
    if store is None:
        return 1
    try:
        store.export(args.run, destination)
    except FileExistsError:
        print(f"error: {destination} already exists", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error, StorageError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(destination)
    return 0


def _when(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _read(args: argparse.Namespace, config: Config) -> int:
    devices = _ezo_devices(args, config)
    if devices is None:
        return 1

    with _controller(config, devices, auto_read=args.follow) as (controller, problems):
        for problem in problems:
            _print(problem)
        if args.follow:
            _follow(controller)
            return 0
        results = controller.read_once()
        for result in results:
            _print(result)
        failed = problems or any(r.outcome is not Outcome.OK for r in results)
        return 1 if failed else 0


def _ezo_cal(args: argparse.Namespace, config: Config) -> int:
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

    with _controller(config, [device]) as (controller, problems):
        # Calibrating the wrong circuit would corrupt it, so the startup type
        # check has to pass first.
        if problems:
            for problem in problems:
                _print(problem)
            return 1
        port = controller.reader.channels[0].port
        try:
            if action == "clear":
                controller.wait(controller.run_job(device.name, clear_calibration_steps(port)))
            elif action != "status":
                steps = calibrate_steps(family, port, action, args.value)
                controller.wait(controller.run_job(device.name, steps))
                if family.name == "ph" and action == "mid":
                    print(
                        "note: a mid-point calibration clears the low and high points; "
                        "calibrate those after it",
                        file=sys.stderr,
                    )
            status = controller.wait(
                controller.run_job(device.name, calibration_status_steps(port))
            )
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        except EzoStatusError as e:
            print(f"error: {device.name} reported {e.outcome.value}", file=sys.stderr)
            return 1
        except EzoDeviceError as e:
            print(f"error: {device.name}: {e}", file=sys.stderr)
            return 1
        print(f"{device.name}: {status}")
        return 0


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
    mode = reading.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="read every sensor once (default)")
    mode.add_argument("--follow", action="store_true", help="read every ezo_period_s until Ctrl-C")
    reading.set_defaults(func=_device_command(_read))

    recording = commands.add_parser("run", help="record a run until Ctrl-C")
    recording.add_argument("-c", "--config", default=DEFAULT_CONFIG, help=config_help)
    recording.add_argument("--name", required=True, help="a name for the run")
    recording.add_argument("--notes", default="", help="free-text notes stored with the run")
    recording.set_defaults(func=_device_command(_run))

    listing = commands.add_parser("runs", help="list recorded runs")
    listing.add_argument("-c", "--config", default=DEFAULT_CONFIG, help=config_help)
    listing.set_defaults(func=_runs)

    exporting = commands.add_parser("export", help="export a run as a zip of CSV files")
    exporting.add_argument("-c", "--config", default=DEFAULT_CONFIG, help=config_help)
    exporting.add_argument("run", type=int, help="the run's number, from openreactor runs")
    exporting.add_argument("-o", "--output", help="the zip file to write (default run-N.zip)")
    exporting.set_defaults(func=_export)

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
    cal.set_defaults(func=_device_command(_ezo_cal))

    serving = commands.add_parser("serve", help="serve the web API until Ctrl-C")
    serving.add_argument("-c", "--config", default=DEFAULT_CONFIG, help=config_help)
    serving.add_argument("--host", help="the address to bind (default server.host)")
    serving.add_argument("--port", type=int, help="the port to bind (default server.port)")
    serving.set_defaults(func=_serve)

    hashing = commands.add_parser("hash-password", help="hash a password for server.password_hash")
    hashing.set_defaults(func=_hash_password)

    profile = commands.add_parser("profile", help="run profile tools")
    profile_commands = profile.add_subparsers(
        dest="profile_command", required=True, metavar="COMMAND"
    )
    validate = profile_commands.add_parser("validate", help="check a profile file and exit")
    validate.add_argument("profile", help="path to the TOML profile")
    validate.add_argument("-c", "--config", help="also check its channels against this config file")
    validate.add_argument(
        "--dry-run", action="store_true", help="print what happens when, then exit"
    )
    validate.set_defaults(func=_profile_validate)
    return parser


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a host name: it may resolve to anything


def _serve(args: argparse.Namespace) -> int:
    config = _load(args.config)
    if config is None:
        return 1
    host = args.host or config.server.host
    port = args.port or config.server.port
    if config.server.password_hash is None and not _loopback(host):
        print(
            f"error: refusing to serve on {host} without a password: anyone on the network "
            "could control the reactor. Set server.password_hash (openreactor hash-password), "
            "or serve on 127.0.0.1.",
            file=sys.stderr,
        )
        return 1
    from openreactor.service import running
    from openreactor.web import create_app

    config_text = Path(args.config).read_text()

    def start() -> AbstractContextManager[Service]:
        return running(
            config,
            config_text,
            open_port=open_port,
            actuators=actuators(config),
            lock_path=None,  # held below, for the server's whole life
            clock=clock,
            sleep=sleep,
        )

    try:
        # Taken before the port is bound, so a second controller is refused
        # with a plain message instead of a failed startup.
        with ControllerLock(lock_path):
            run_server(create_app(config, start), host, port)
    except LockHeld as e:
        print(
            f"error: another controller holds {e.path} ({e.holder}); stop it first",
            file=sys.stderr,
        )
        return 1
    except OSError as e:
        where = f"{e.filename}: " if e.filename else ""
        print(f"error: {where}{e.strerror or e}", file=sys.stderr)
        return 1
    return 0


def _hash_password(args: argparse.Namespace) -> int:
    if sys.stdin.isatty():
        password = getpass.getpass("Password: ")
        if getpass.getpass("Again: ") != password:
            print("error: the passwords differ", file=sys.stderr)
            return 1
    else:
        password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        print("error: the password is empty", file=sys.stderr)
        return 1
    print(hash_password(password))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
