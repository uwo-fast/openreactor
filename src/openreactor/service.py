"""Carry out every operator action for the API and the UI.

The service never touches the bus itself. Readings come from the
controller, actions go to it as commands, and the run's database writes
happen on the controller's thread. Listing and exporting runs use read-only
connections of their own, so request threads never write.
"""

from __future__ import annotations

import math
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypeVar

from crumbs_i2c import Bus, LinuxBus

from openreactor.config import SLICE_KINDS, ChannelConfig, Config, DeviceConfig, setpoint_range
from openreactor.controller import Actuator, Controller, Event
from openreactor.dcmt import DcmtSlice, DcmtStatus, duty_to_pwm, pwm_to_duty
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
from openreactor.profile import ProfileError, profile_from_bytes
from openreactor.rlht import SendFailed, SliceBusy, SliceError, SliceStatus
from openreactor.runner import ProfileRunner
from openreactor.slices import Slice, opened_slices, release
from openreactor.storage import Recorder, Run, StorageError, Store, default_path

_T = TypeVar("_T")

# How long a request waits for the controller to finish an action.
ACTION_TIMEOUT_S = 30.0
# How long status waits for the slices' state. It is well inside status's
# own timeout, so a busy controller shows as busy instead of not answering.
SLICE_STATUS_TIMEOUT_S = 2.0


def _wait(future: Future[_T]) -> _T:
    """Wait for the action and return its result.

    An action the controller has not started within the timeout is
    cancelled, so it never runs after the caller gave up; one already
    running is left to finish.
    """
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


def setpoint_limit(ch: ChannelConfig) -> float:
    """Return the highest setpoint, in °C, that an RLHT channel accepts."""
    return setpoint_range("rlht", ch)[1]


def _problem_text(p: Result) -> str:
    """Return a start-up problem as its outcome, then its detail if any."""
    return f"{p.outcome.value}: {p.detail}" if p.detail else p.outcome.value


def _end_status(recorder: Recorder | None, ended: str = "stopped") -> str:
    """Return how a run ends: ``ended``, or "interrupted" if it stopped recording."""
    failed = recorder.failed if recorder is not None else None
    return ended if failed is None else "interrupted"


@dataclass(frozen=True)
class ProfileState:
    run: int
    name: str
    elapsed_s: float
    duration_s: float


class NotFound(Exception):
    """No such channel, device or run."""


class Conflict(Exception):
    """The action does not fit the current state.

    Examples: a run already recording, or a slice e-stopped or read-only.
    """


class Unavailable(Exception):
    """The action needs something this build or this hardware lacks."""


class Unreachable(Exception):
    """The device is configured but cannot be reached now."""


class DeviceFailed(Exception):
    """The device was sent the command, and the send failed."""


class Invalid(Exception):
    """The request's values are wrong, such as a calibration point the device lacks."""


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
        slices: Sequence[Slice] = (),
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
        # The profile driving the current run, if one does.
        self._runner: ProfileRunner | None = None
        self._ezo = {d.name: d for d in config.devices if d.kind in FAMILIES}
        # Where each published channel comes from: (device, kind).
        self._sources: dict[str, tuple[str, str]] = {
            d: (d, dev.kind) for d, dev in self._ezo.items()
        }
        for d in config.devices:
            if d.kind in SLICE_KINDS:
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
                        self._latest[name] = replace(state, kind=kind, outcome=item.outcome.value)
                return
            for v in item.values:
                name = f"{item.channel}.{v.field}" if v.field else item.channel
                self._latest[name] = ChannelState(name, device, kind, v.unit, v.value, at, "ok")

    # Status and channels

    def devices(self) -> list[DeviceState]:
        states: list[DeviceState] = []
        for d in self.config.devices:
            s = self._slices.get(d.name)
            if d.name in self._problems:
                status = _problem_text(self._problems[d.name])
            elif s is not None and s.unreachable:
                status = "unreachable: 3 polls failed in a row"
            elif s is not None and s.estop:
                status = "e-stop held on the slice"
            elif isinstance(s, DcmtSlice) and s.held:
                status = "watchdog trip held: clear it to drive the motors"
            elif s is not None and s.read_only:
                status = "read-only: the slice has no command watchdog"
            else:
                status = "ok"
            states.append(DeviceState(d.name, d.kind, status))
        return states

    def slices(self) -> list[SliceStatus]:
        """Return each configured RLHT slice as the controller last saw it.

        It is read on the controller's thread, so each status is consistent.
        """

        def why(name: str) -> str | None:
            p = self._problems.get(name)
            if p is None:
                return None
            return _problem_text(p)

        def snapshot() -> list[SliceStatus]:
            out: list[SliceStatus] = []
            for d in self.config.devices:
                if d.kind != "rlht":
                    continue
                s = self._slices.get(d.name)
                if s is not None and not isinstance(s, DcmtSlice):
                    out.append(s.status(why(d.name) if not s.started else None))
                else:
                    out.append(SliceStatus(d.name, d.address, why(d.name) or "not in use"))
            return out

        future = self.controller.call(snapshot)
        try:
            return future.result(timeout=SLICE_STATUS_TIMEOUT_S)
        except TimeoutError:
            # The rest of status needs no controller, so do not fail it whole.
            future.cancel()
            return [
                SliceStatus(d.name, d.address, "unknown: the controller did not answer in time")
                for d in self.config.devices
                if d.kind == "rlht"
            ]

    def dcmt_slices(self) -> list[DcmtStatus]:
        """Return each configured DCMT slice as the controller last saw it.

        It is read on the controller's thread, so each status is consistent.
        """

        def snapshot() -> list[DcmtStatus]:
            out: list[DcmtStatus] = []
            for d in self.config.devices:
                if d.kind != "dcmt":
                    continue
                s = self._slices.get(d.name)
                p = self._problems.get(d.name)
                why = _problem_text(p) if p is not None else None
                if isinstance(s, DcmtSlice):
                    out.append(s.status(why if not s.started else None))
                else:
                    out.append(DcmtStatus(d.name, d.address, why or "not in use"))
            return out

        future = self.controller.call(snapshot)
        try:
            return future.result(timeout=SLICE_STATUS_TIMEOUT_S)
        except TimeoutError:
            future.cancel()
            return [
                DcmtStatus(d.name, d.address, "unknown: the controller did not answer in time")
                for d in self.config.devices
                if d.kind == "dcmt"
            ]

    def channels(self) -> list[ChannelState]:
        with self._lock:
            return sorted(self._latest.values(), key=lambda c: c.name)

    def current_run(self) -> int | None:
        return self._run

    def recording_failed(self) -> str | None:
        """Return why the current run stopped recording, or None if it did not."""
        recorder = self._recorder
        return recorder.failed if recorder is not None else None

    def set_setpoint(self, channel: str, value: float) -> None:
        """Set an actuated channel's setpoint.

        On an RLHT channel it is a temperature in °C, rounded to a tenth of
        a degree. It stays the desired setpoint, sent again after a trip or
        a reboot, until it is set again or stop-all or an e-stop zeroes it.

        On a DCMT channel it is the motor's duty in percent, -100 to 100,
        rounded to the slice's PWM steps; 0 brakes the motor. Nothing sends
        it again: an e-stop, a trip, a reboot or stop-all stops the motor
        until it is set again.
        """
        runner = self._runner
        if runner is not None and channel in runner.profile.channels:
            raise Conflict(
                f"profile {runner.profile.name!r} drives {channel}; abort the profile first"
            )
        for d in self.config.devices:
            for ch in d.channels:
                if ch.name == channel:
                    if d.kind == "dcmt":
                        self._set_slice_duty(d, ch, value)
                    else:
                        self._set_slice_setpoint(d, ch, value)
                    return
        raise NotFound(f"no actuated channel {channel!r}")

    def channel_kind(self, channel: str) -> str | None:
        """Return the kind of slice an actuated channel is on, or None."""
        for d in self.config.devices:
            if any(ch.name == channel for ch in d.channels):
                return d.kind
        return None

    def _set_slice_duty(self, d: DeviceConfig, ch: ChannelConfig, value: float) -> None:
        if not math.isfinite(value) or not -100 <= value <= 100:
            raise Invalid(f"{ch.name}: the duty must be from -100 to 100 %")
        pwm = duty_to_pwm(value)
        s = self._slice(d)
        assert isinstance(s, DcmtSlice)
        self._drive(d, ch, f"{pwm_to_duty(pwm):g} %", lambda: s.set_duty(ch.output, pwm))

    def _slice(self, d: DeviceConfig) -> Slice:
        s = self._slices.get(d.name)
        if s is None:
            problem = self._problems.get(d.name)
            why = f": {problem.detail}" if problem is not None and problem.detail else ""
            raise Unreachable(f"{d.name} is not in use{why}")
        return s

    def _drive(
        self, d: DeviceConfig, ch: ChannelConfig, details: str, send: Callable[[], None]
    ) -> None:
        """Run ``send`` on the controller's thread and log it as a setpoint event."""

        def event(result: str = "ok") -> Event:
            return Event(
                self._wall(),
                "user",
                "setpoint",
                device=d.name,
                channel=ch.name,
                details=details,
                result=result,
            )

        def work() -> None:
            try:
                send()
            except SendFailed as e:
                self.controller.publish(event(f"error: {e}"))
                raise
            self.controller.publish(event())

        try:
            _wait(self.controller.call(work))
        except SliceBusy as e:
            raise Conflict(str(e)) from None
        except SendFailed as e:
            # It may have reached the slice: the next polls compare what the
            # slice runs with what is wanted, and send the safe state if they
            # differ.
            raise DeviceFailed(f"{e}; it may have reached the slice") from None
        except SliceError as e:
            raise Unreachable(str(e)) from None

    def _set_slice_setpoint(self, d: DeviceConfig, ch: ChannelConfig, value: float) -> None:
        limit = setpoint_limit(ch)
        if not math.isfinite(value) or value < 0:
            raise Invalid(f"{ch.name}: the setpoint must be 0 °C or more")
        deci = round(value * 10)
        # Compare in tenths, as sent: 80.06 goes as 80.1.
        if deci > math.floor(limit * 10 + 1e-9):
            what = "its max_setpoint" if ch.max_setpoint is not None else "the most the slice takes"
            raise Invalid(f"{ch.name}: {deci / 10:g} °C is above {what}, {limit:g} °C")
        s = self._slice(d)
        assert not isinstance(s, DcmtSlice)
        self._drive(d, ch, f"{deci / 10:g} °C", lambda: s.set_setpoint(ch.output, deci))

    def clear_trip(self, device: str) -> list[Event]:
        """Clear a latched watchdog trip on a DCMT slice; its motors stay stopped."""
        d = next((d for d in self.config.devices if d.name == device), None)
        if d is None or d.kind != "dcmt":
            raise NotFound(f"no DCMT device {device!r}")
        s = self._slice(d)
        assert isinstance(s, DcmtSlice)

        def work() -> list[Event]:
            events = s.clear_trip()
            for e in events:
                self.controller.publish(e)
            return events

        try:
            return _wait(self.controller.call(work))
        except SliceBusy as e:
            raise Conflict(str(e)) from None
        except SendFailed as e:
            raise DeviceFailed(f"{e}; the trip may or may not be cleared") from None
        except SliceError as e:
            raise Unreachable(str(e)) from None

    # Stop-all

    def stop_all(self) -> list[Event]:
        # Never cancelled: a stop-all that reports late has still been sent.
        future = self.controller.stop_all("user")
        # After it, so stop-all goes first: the profile's channels are then
        # already at 0, and it ends without sending anything more.
        self.controller.call(lambda: self._abort_profile("stop-all was sent"))
        try:
            return future.result(timeout=ACTION_TIMEOUT_S)
        except TimeoutError:
            raise TimeoutError("stop-all was queued but has not reported back") from None

    def stop_now(self) -> None:
        """Queue stop-all without waiting for it.

        A signal handler calls this before the server drains its connections.
        """
        self.controller.stop_all("system")

    # Runs

    def start_run(self, name: str, notes: str = "") -> int:
        return _wait(self.controller.call(lambda: self._start_run(name, notes)))

    def _start_run(self, name: str, notes: str, profile: str | None = None) -> int:
        """Start recording a run; call it on the controller's thread."""
        if self._run is not None:
            failed = self.recording_failed()
            if failed is not None:
                raise Conflict(f"run {self._run} stopped recording ({failed}); stop it first")
            raise Conflict(f"run {self._run} is already recording")
        if self._store is None:
            self._store = Store(self._database)
        store = self._store
        store.setup()
        # The controller lock is held for the service's life, so a run still
        # marked running was left by a process that died.
        store.interrupt_stale_runs()
        run = store.start_run(name, self._config_text, notes, profile)
        self._recorder = Recorder(store, run, dict(self._sources), wall=self._wall)
        store.recording()
        self.controller.subscribe(self._recorder)
        self._run = run
        return run

    def stop_run(self) -> tuple[int, str]:
        """Stop the recording run; a profile driving it is aborted first."""

        def stop() -> tuple[int, str]:
            return self._abort_profile("the run was stopped") or self._end_run("stopped")

        return _wait(self.controller.call(stop))

    def _end_run(self, ended: str) -> tuple[int, str]:
        """End the recording run; call it on the controller's thread."""
        if self._run is None or self._store is None or self._recorder is None:
            raise Conflict("no run is recording")
        run, store, recorder = self._run, self._store, self._recorder
        self.controller.unsubscribe(recorder)
        status = _end_status(recorder, ended)
        store.setup()
        store.end_run(run, status)
        self._run, self._recorder = None, None
        return run, status

    # Profiles

    def start_profile(self, text: str, name: str = "", notes: str = "") -> int:
        """Start a run that a profile drives, and return the run's id.

        The profile is checked against the config's actuated channels and
        their ranges; the run is named after the profile unless ``name`` is
        given. It starts at once, and every channel it touched goes safe
        when it ends or is aborted.
        """
        actuated = {
            ch.name: (d, ch, self._slices.get(d.name))
            for d in self.config.devices
            if d.kind in SLICE_KINDS
            for ch in d.channels
        }
        limits = {n: setpoint_range(d.kind, ch) for n, (d, ch, _) in actuated.items()}
        try:
            profile = profile_from_bytes(text.encode(), actuated, "profile", limits)
        except ProfileError as e:
            raise Invalid("; ".join(str(p) for p in e.problems)) from None
        channels: dict[str, tuple[DeviceConfig, ChannelConfig, Slice]] = {}
        for n in profile.channels:
            d, ch, s = actuated[n]
            if s is None or not s.started:
                raise Unreachable(f"{n}: {d.name} is not in use")
            channels[n] = (d, ch, s)

        def start() -> int:
            # A running profile's run is recording, so this refuses a second.
            run = self._start_run(name or profile.name, notes or profile.notes, text)
            runner = ProfileRunner(
                profile,
                channels,
                step_s=self.config.controller.slice_poll_s,
                finished=self._profile_finished,
                wall=self._wall,
            )
            self._runner = runner
            self.controller.polled.append(runner)
            self.controller.publish(
                Event(self._wall(), "user", "profile-start", details=f"{profile.name!r}")
            )
            return run

        return _wait(self.controller.call(start))

    def _profile_finished(self, ended: str, why: str) -> None:
        """End the profile's run; the runner calls it on the controller's thread.

        Its last events are published after it returns, so the run ends on
        the next tick, with them recorded.
        """
        runner = self._runner

        def end() -> None:
            if runner is not None and self._runner is runner:
                self._drop_profile(ended)

        self.controller.call(end)

    def _drop_profile(self, ended: str) -> tuple[int, str] | None:
        """Take the runner off the controller and end its run."""
        runner, self._runner = self._runner, None
        if runner is not None and runner in self.controller.polled:
            self.controller.polled.remove(runner)
        return self._end_run(ended) if self._run is not None else None

    def _abort_profile(self, why: str) -> tuple[int, str] | None:
        """Abort a running profile and end its run, stopped.

        Call it on the controller's thread. Every channel the profile
        touched is sent its safe state first.
        """
        runner = self._runner
        if runner is None:
            return None
        for item in runner.abort(why):
            self.controller.publish(item)
        return self._drop_profile("stopped")

    def abort_profile(self) -> tuple[int, str]:
        """Abort the running profile and end its run, stopped."""

        def abort() -> tuple[int, str]:
            ended = self._abort_profile("aborted by the operator")
            if ended is None:
                raise Conflict("no profile is running")
            return ended

        return _wait(self.controller.call(abort))

    def profile(self) -> ProfileState | None:
        """Return the running profile, if any."""

        def state() -> ProfileState | None:
            runner = self._runner
            if runner is None or self._run is None:
                return None
            return ProfileState(
                self._run,
                runner.profile.name,
                round(runner.elapsed(self.controller.now()), 1),
                runner.profile.duration,
            )

        return _wait(self.controller.call(state))

    def runs(self) -> list[Run]:
        if not self._database.exists():
            return []
        with closing(Store(self._database, read_only=True)) as store:
            return store.runs()

    def export(self, run: int) -> bytes:
        if not self._database.exists():
            raise NotFound(f"no run {run}")
        with (
            closing(Store(self._database, read_only=True)) as store,
            tempfile.TemporaryDirectory() as d,
        ):
            out = Path(d) / "run.zip"
            try:
                store.export(run, out)
            except StorageError as e:
                if store.run(run) is None:
                    raise NotFound(f"no run {run}") from e
                raise
            return out.read_bytes()

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
        """Send stop-all, release the DCMTs, end a recording run, and close
        the database.

        This runs before the circuits are closed.
        """
        try:
            # Stop-all first, so its events are part of the run. The
            # controller's thread has stopped after this, so ending the run
            # here cannot race it, and the DCMTs can be released. If it did
            # not stop, they are not: their watchdogs trip instead.
            self.controller.close()
            release(list(self._slices.values()))
        finally:
            try:
                if self._run is not None and self._store is not None:
                    self._store.setup()
                    self._store.end_run(self._run, _end_status(self._recorder))
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
    on_event: Callable[[Result | Event], None] | None = None,
) -> Iterator[Service]:
    """Start the controller and yield the service.

    This takes the controller lock, opens and checks the circuits, and
    starts the slices and the controller's thread. On the way out,
    stop-all is sent before anything closes. ``lock_path=None`` means the
    caller already holds the lock. ``on_event`` gets each start-up problem
    and event, then every event the controller publishes.
    """
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
                    polled=[s for s in slices if s.started],
                    ezo_period_s=config.controller.ezo_period_s,
                    auto_read=True,
                    clock=clock,
                    sleep=sleep,
                )
                service = Service(
                    config, config_text, controller, database, problems, slices=slices
                )
                if on_event is not None:
                    for item in problems:
                        on_event(item)
                    controller.subscribe(
                        lambda item: on_event(item) if isinstance(item, Event) else None
                    )
                controller.start()
                try:
                    yield service
                finally:
                    service.close()
        finally:
            for ch in channels:
                ch.port.close()
