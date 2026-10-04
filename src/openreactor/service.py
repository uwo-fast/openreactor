"""The service layer: every operator action, for the API and the UI alike.

It never touches the bus itself. Readings come from the controller, actions
go to it as commands, and the run's database writes happen on the
controller's thread. Listing and exporting runs use read-only connections of
their own, so request threads never write.
"""

from __future__ import annotations

import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from crumbs_i2c import Bus, LinuxBus

from openreactor.config import Config, DeviceConfig
from openreactor.controller import Actuator, Controller, Event
from openreactor.ezo import (
    FAMILIES,
    EzoChannel,
    EzoDeviceError,
    EzoPort,
    EzoReader,
    Outcome,
    Result,
    Steps,
    calibrate_steps,
    calibration_status_steps,
    check_calibration,
    clear_calibration_steps,
)
from openreactor.lock import LOCK_PATH, ControllerLock
from openreactor.rlht import RlhtSlice, opened_slices
from openreactor.storage import Recorder, Run, StorageError, Store, default_path

_T = TypeVar("_T")

# How long a request waits for the controller to finish an action.
ACTION_TIMEOUT_S = 30.0


def _wait(future: Future[_T]) -> _T:
    """The action's result. One the controller has not started within the
    timeout is cancelled, so it never runs after the caller gave up; one
    already running is let finish."""
    try:
        return future.result(timeout=ACTION_TIMEOUT_S)
    except TimeoutError:
        if future.cancel():
            raise TimeoutError(
                "the controller did not get to it in time; nothing was done"
            ) from None
    try:
        return future.result(timeout=ACTION_TIMEOUT_S)
    except TimeoutError:
        raise TimeoutError("the controller started this but has not finished it") from None


class NotFound(Exception):
    """No such channel, device or run."""


class Conflict(Exception):
    """The action does not fit the current state (a run already running)."""


class Unavailable(Exception):
    """The action needs something this build or this hardware lacks."""


class Invalid(Exception):
    """The request's values are wrong (a calibration point the device lacks)."""


@dataclass(frozen=True)
class ChannelState:
    name: str
    device: str
    kind: str
    unit: str
    value: float | None
    time: float | None
    outcome: str


@dataclass(frozen=True)
class DeviceState:
    name: str
    kind: str
    status: str  # "ok", or why it is out of use


class Service:
    def __init__(
        self,
        config: Config,
        config_text: str,
        controller: Controller,
        database: Path,
        problems: Sequence[Result | Event] = (),
        wall: Callable[[], float] = time.time,
        slices: Sequence[RlhtSlice] = (),
    ):
        self.config = config
        self._config_text = config_text
        self.controller = controller
        self._database = database
        self._wall = wall
        self._lock = threading.Lock()
        self._latest: dict[str, ChannelState] = {}
        self._problems = {p.channel: p for p in problems if isinstance(p, Result)}
        self._slices = {s.name: s for s in slices}
        self._store: Store | None = None
        self._recorder: Recorder | None = None
        self._run: int | None = None
        self._ezo = {d.name: d for d in config.devices if d.kind in FAMILIES}
        # Where each published channel comes from: (device, kind).
        self._sources: dict[str, tuple[str, str]] = {
            d: (d, dev.kind) for d, dev in self._ezo.items()
        }
        for d in config.devices:
            if d.kind == "rlht":
                self._sources.update({ch.name: (d.name, d.kind) for ch in d.channels})
        controller.subscribe(self._remember)

    # Called on the controller's thread

    def _remember(self, item: Result | Event) -> None:
        if not isinstance(item, Result):
            return
        device, kind = self._sources.get(item.channel, (item.channel, ""))
        at = self._wall()
        with self._lock:
            if item.outcome is not Outcome.OK:
                for name, state in list(self._latest.items()):
                    if name == item.channel or name.startswith(f"{item.channel}."):
                        self._latest[name] = ChannelState(
                            name,
                            state.device,
                            kind,
                            state.unit,
                            state.value,
                            state.time,
                            item.outcome.value,
                        )
                return
            for v in item.values:
                name = f"{item.channel}.{v.field}" if v.field else item.channel
                self._latest[name] = ChannelState(name, device, kind, v.unit, v.value, at, "ok")

    # Status and channels

    def devices(self) -> list[DeviceState]:
        states: list[DeviceState] = []
        for d in self.config.devices:
            if d.kind == "dcmt":
                status = "DCMT slices are not supported yet"
            elif d.name in self._problems:
                p = self._problems[d.name]
                status = f"{p.outcome.value}: {p.detail}" if p.detail else p.outcome.value
            elif d.name in self._slices and self._slices[d.name].read_only:
                status = "read-only: the slice has no command watchdog"
            else:
                status = "ok"
            states.append(DeviceState(d.name, d.kind, status))
        return states

    def channels(self) -> list[ChannelState]:
        with self._lock:
            return sorted(self._latest.values(), key=lambda c: c.name)

    def current_run(self) -> int | None:
        return self._run

    def recording_failed(self) -> str | None:
        """Why the current run stopped recording, if it did."""
        recorder = self._recorder
        return recorder.failed if recorder is not None else None

    def set_setpoint(self, channel: str, value: float) -> None:
        for d in self.config.devices:
            if any(ch.name == channel for ch in d.channels):
                raise Unavailable(
                    f"{channel} is on a {d.kind} slice; setpoints on slices are not supported yet"
                )
        raise NotFound(f"no actuated channel {channel!r}")

    # Stop-all

    def stop_all(self) -> list[Event]:
        # Never cancelled: a stop-all that reports late has still been sent.
        try:
            return self.controller.stop_all("user").result(timeout=ACTION_TIMEOUT_S)
        except TimeoutError:
            raise TimeoutError("stop-all was queued but has not reported back") from None

    def stop_now(self) -> None:
        """Queue stop-all without waiting: for a signal handler, before the
        server drains its connections."""
        self.controller.stop_all("system")

    # Runs

    def start_run(self, name: str, notes: str = "") -> int:
        def start() -> int:
            if self._run is not None:
                failed = self.recording_failed()
                if failed is not None:
                    raise Conflict(f"run {self._run} stopped recording ({failed}); stop it first")
                raise Conflict(f"run {self._run} is already recording")
            if self._store is None:
                self._store = Store(self._database)
            store = self._store
            store.setup()
            # The controller lock is held for the service's life, so a run
            # still marked running was left by a process that died.
            store.interrupt_stale_runs()
            run = store.start_run(name, self._config_text, notes)
            self._recorder = Recorder(store, run, dict(self._sources), wall=self._wall)
            store.recording()
            self.controller.subscribe(self._recorder)
            self._run = run
            return run

        return _wait(self.controller.call(start))

    def stop_run(self) -> tuple[int, str]:
        def stop() -> tuple[int, str]:
            if self._run is None or self._store is None or self._recorder is None:
                raise Conflict("no run is recording")
            run, store, recorder = self._run, self._store, self._recorder
            self.controller.unsubscribe(recorder)
            status = "stopped" if recorder.failed is None else "interrupted"
            store.setup()
            store.end_run(run, status)
            self._run, self._recorder = None, None
            return run, status

        return _wait(self.controller.call(stop))

    def runs(self) -> list[Run]:
        if not self._database.exists():
            return []
        store = Store(self._database, read_only=True)
        try:
            return store.runs()
        finally:
            store.close()

    def export(self, run: int) -> bytes:
        if not self._database.exists():
            raise NotFound(f"no run {run}")
        store = Store(self._database, read_only=True)
        try:
            with tempfile.TemporaryDirectory() as d:
                out = Path(d) / "run.zip"
                try:
                    store.export(run, out)
                except StorageError as e:
                    if store.run(run) is None:
                        raise NotFound(f"no run {run}") from e
                    raise
                return out.read_bytes()
        finally:
            store.close()

    # EZO calibration

    def _ezo_port(self, device: str) -> tuple[DeviceConfig, EzoPort]:
        if device not in self._ezo:
            raise NotFound(f"no EZO device {device!r}")
        for ch in self.controller.reader.channels:
            if ch.name == device and ch.enabled:
                return ch.device, ch.port
        # Configured, but it did not open or failed its startup check.
        problem = self._problems.get(device)
        why = f": {problem.detail}" if problem is not None and problem.detail else ""
        raise Unavailable(f"{device} is out of use since startup{why}")

    def calibration(self, device: str) -> str:
        _, port = self._ezo_port(device)
        return self._job(device, calibration_status_steps(port))

    def calibrate(self, device: str, point: str, value: float | None) -> str:
        config, port = self._ezo_port(device)
        family = FAMILIES[config.kind]
        try:
            check_calibration(family, point, value)
        except ValueError as e:
            raise Invalid(str(e)) from e
        self._job(device, calibrate_steps(family, port, point, value))
        return self.calibration(device)

    def clear_calibration(self, device: str) -> str:
        _, port = self._ezo_port(device)
        self._job(device, clear_calibration_steps(port))
        return self.calibration(device)

    def _job(self, device: str, steps: Steps[_T]) -> _T:
        return _wait(self.controller.run_job(device, steps))

    # Shutdown

    def close(self) -> None:
        """Stop-all, then end a recording run, then close the database.
        Called before the circuits are closed."""
        try:
            # Stop-all first, so its events are part of the run. The
            # controller's thread has stopped after this, so ending the run
            # here cannot race it.
            self.controller.close()
        finally:
            try:
                if self._run is not None and self._store is not None:
                    failed = self._recorder.failed if self._recorder else None
                    self._store.setup()
                    self._store.end_run(self._run, "stopped" if failed is None else "interrupted")
                    self._run = None
            finally:
                if self._store is not None:
                    self._store.close()


def _open_port(device: DeviceConfig) -> EzoPort:
    from openreactor.ezo import DriverPort

    return DriverPort(device.kind, device.bus, device.address)


@contextmanager
def running(
    config: Config,
    config_text: str,
    *,
    open_port: Callable[[DeviceConfig], EzoPort] = _open_port,
    actuators: Sequence[Actuator] = (),
    open_bus: Callable[[str], Bus] = LinuxBus,
    lock_path: Path | None = LOCK_PATH,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[Service]:
    """Take the controller lock, open and check the circuits, start the RLHT
    slices, start the controller on its own thread, and yield the service. On
    the way out, stop-all is sent before anything closes. ``lock_path=None``
    means the caller already holds the lock."""
    database = Path(config.storage.database) if config.storage.database else default_path()
    with ControllerLock(lock_path) if lock_path is not None else nullcontext():
        channels: list[EzoChannel] = []
        problems: list[Result | Event] = []
        try:
            for d in config.devices:
                if d.kind not in FAMILIES:
                    continue
                try:
                    channels.append(EzoChannel(d, open_port(d)))
                except EzoDeviceError as e:
                    problems.append(Result(d.name, Outcome.ERROR, detail=str(e)))
            reader = EzoReader(channels, clock=clock, sleep=sleep)
            problems += reader.prepare()
            with opened_slices(
                config.devices,
                watchdog_timeout_ms=config.controller.watchdog_timeout_ms,
                poll_s=config.controller.slice_poll_s,
                open_bus=open_bus,
                clock=clock,
                sleep=sleep,
            ) as (slices, reports):
                problems += reports
                controller = Controller(
                    reader,
                    [*actuators, *slices],
                    polled=slices,
                    ezo_period_s=config.controller.ezo_period_s,
                    auto_read=True,
                    clock=clock,
                    sleep=sleep,
                )
                service = Service(
                    config, config_text, controller, database, problems, slices=slices
                )
                controller.start()
                try:
                    yield service
                finally:
                    service.close()
        finally:
            for ch in channels:
                ch.port.close()
