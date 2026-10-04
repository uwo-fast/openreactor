"""The controller: the only owner of the bus.

Everything that touches a device goes through it. It runs a 100 ms tick on a
monotonic clock and never blocks inside a tick: EZO reads are split-phase,
and multi-step commands (calibration, queries) are step generators it resumes
on later ticks. Other threads talk to it through a command queue and get a
``Future`` back.

Stop-all is the one thing that must always work: it sends every actuator its
safe state before anything else can run, a failing actuator or listener does
not stop the others, and ``close`` sends it and nothing else.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from openreactor.ezo import EzoReader, Result, Steps

TICK_S = 0.1

log = logging.getLogger(__name__)

_T = TypeVar("_T")


class ControllerClosed(Exception):
    """The controller closed before this command finished."""


@dataclass(frozen=True)
class Event:
    """Something that happened, for the run log: a stop-all, a command."""

    time: float  # wall clock, seconds since the epoch, UTC
    source: str  # "user", "profile" or "system"
    kind: str
    device: str | None = None
    channel: str | None = None
    details: str = ""
    result: str = "ok"


class Actuator(Protocol):
    """An output the controller can put into its safe state."""

    name: str

    def send_safe(self) -> None: ...


Listener = Callable[["Result | Event"], None]


@dataclass
class _Job:
    channel: str
    steps: Steps[Any]
    future: Future[Any]
    resume_at: float | None = None  # None until the job has started


@dataclass
class _CycleRequest:
    names: set[str]
    future: Future[list[Result]]
    # The circuits whose result this request still waits for: the reads it
    # started, and reads already in flight that it joined.
    awaiting: set[str] = field(default_factory=set[str])
    results: list[Result] = field(default_factory=list[Result])
    started: bool = False
    rearmed: bool = False


class Controller:
    """``step`` runs one tick. ``run`` ticks on the calling thread until told
    to stop; ``start`` runs it on a thread of its own instead."""

    def __init__(
        self,
        reader: EzoReader,
        actuators: Sequence[Actuator] = (),
        *,
        ezo_period_s: float,
        auto_read: bool = False,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], float] = time.time,
    ):
        self.reader = reader
        self.actuators = list(actuators)
        self._period = ezo_period_s
        self._auto_read = auto_read
        self._clock = clock
        self._sleep = sleep
        self._wall = wall
        self._commands: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._jobs: list[_Job] = []
        self._cycles: list[_CycleRequest] = []
        self._listeners: list[Listener] = []
        self._next_auto_cycle = clock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False

    # Called from any thread

    def subscribe(self, listener: Listener) -> None:
        self._commands.put(lambda: self._listeners.append(listener))

    def stop_all(self, source: str = "user") -> Future[list[Event]]:
        future: Future[list[Event]] = Future()

        def command() -> None:
            self._stop_all(source, future)

        self._commands.put(command)
        return future

    def run_job(self, channel: str, steps: Steps[_T]) -> Future[_T]:
        """Run a multi-step command on one EZO circuit. Reads of that circuit
        wait until it finishes, so the two never interleave on the device."""
        future: Future[_T] = Future()
        self._commands.put(lambda: self._jobs.append(_Job(channel, steps, future)))
        return future

    def read_cycle(self, names: Iterable[str] | None = None) -> Future[list[Result]]:
        """Read ``names`` (default every circuit) once, giving a NOT_READY
        circuit one retry a full delay later."""
        wanted = set(names) if names is not None else {c.name for c in self.reader.channels}
        request = _CycleRequest(wanted, Future())
        self._commands.put(lambda: self._cycles.append(request))
        return request.future

    # The tick

    def step(self) -> None:
        """One tick. Never sleeps and never waits on a device."""
        self._drain_commands()
        now = self._clock()
        self._advance_jobs(now)
        self._advance_reads(now)

    def _drain_commands(self) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            command()

    def _publish(self, item: Result | Event) -> None:
        for listener in self._listeners:
            try:
                listener(item)
            except Exception:
                # A listener (a printer, the database) must never stop
                # control or stop-all.
                log.exception("a listener failed on %r", item)

    def _stop_all(self, source: str, future: Future[list[Event]] | None = None) -> list[Event]:
        """Send every actuator its safe state, then report. An actuator that
        fails, or an interrupt while one is sent, never skips the others."""
        events: list[Event] = []
        interrupted: BaseException | None = None
        for actuator in self.actuators:
            try:
                actuator.send_safe()
                result = "ok"
            except Exception as e:
                result = f"error: {e}"
            except BaseException as e:  # Ctrl-C, SIGTERM: finish, then re-raise
                interrupted = interrupted or e
                result = f"interrupted: {type(e).__name__}"
            events.append(
                Event(self._wall(), source, "stop-all", device=actuator.name, result=result)
            )
        if not self.actuators:
            events.append(
                Event(self._wall(), source, "stop-all", details="no actuators configured")
            )
        if future is not None:
            future.set_result(events)
        for event in events:
            self._publish(event)
        if interrupted is not None:
            raise interrupted
        return events

    def _busy(self) -> set[str]:
        return {job.channel for job in self._jobs if job.resume_at is not None}

    def _advance_jobs(self, now: float) -> None:
        busy = self._busy()
        pending = self.reader.pending()
        for job in list(self._jobs):
            if job.resume_at is None:
                if job.channel in busy or job.channel in pending:
                    continue
                busy.add(job.channel)
            elif now < job.resume_at:
                continue
            try:
                wait = next(job.steps)
            except StopIteration as done:
                self._jobs.remove(job)
                job.future.set_result(done.value)
                continue
            except Exception as e:
                self._jobs.remove(job)
                job.future.set_exception(e)
                continue
            job.resume_at = now + wait

    def _advance_reads(self, now: float) -> None:
        busy = self._busy()
        for request in self._cycles:
            if not request.started:
                request.started = True
                request.results += self.reader.begin(request.names, skip=busy)
                # Wait for every requested circuit now in flight, whether this
                # request started its read or joined one already pending.
                request.awaiting = request.names & self.reader.pending()
        if self._auto_read and now + 1e-6 >= self._next_auto_cycle:
            for result in self.reader.begin(skip=busy):
                self._publish(result)
            self._next_auto_cycle = max(self._next_auto_cycle + self._period, now)

        for result in self.reader.collect():
            self._publish(result)
            for request in self._cycles:
                if result.channel in request.awaiting:
                    request.awaiting.discard(result.channel)
                    request.results.append(result)

        waiting = self.reader.waiting()
        for request in list(self._cycles):
            if not request.awaiting:
                self._cycles.remove(request)
                request.future.set_result(request.results)
            elif request.awaiting <= waiting and not request.rearmed:
                # Only NOT_READY retries are left: read them again a full
                # delay from now, once.
                request.rearmed = True
                self.reader.rearm()

    # Driving the tick

    def run(self, until: Callable[[], bool], *, keep_going: bool = False) -> None:
        """Tick every 100 ms on the calling thread until ``until()``. With
        ``keep_going``, a tick that raises is logged and ticking continues."""
        # Ticks are start + n * TICK_S, so they never drift. A tick that
        # overruns is not made up; the next starts on the next boundary.
        start = self._clock()
        n = 0
        while not until():
            try:
                self.step()
            except Exception:
                if not keep_going:
                    raise
                log.exception("a controller tick failed")
            n += 1
            now = self._clock()
            if start + n * TICK_S <= now:
                n = int((now - start) / TICK_S) + 1
            self._sleep(start + n * TICK_S - now)

    def wait(self, future: Future[_T]) -> _T:
        """Tick on the calling thread until ``future`` is done. Not for use
        while ``start`` has the controller ticking on its own thread."""
        if self._thread is not None:
            raise RuntimeError("the controller is ticking on its own thread; use the future")
        self.run(future.done)
        return future.result()

    def read_once(self) -> list[Result]:
        """Read every circuit once, on the calling thread. RTDs used for
        compensation are read first, so pH, EC and DO compensate with this
        reading's temperature."""
        channels = [c for c in self.reader.channels if c.enabled]
        sources = {c.device.temp_comp for c in channels if c.device.temp_comp}
        first = self.wait(self.read_cycle(sources)) if sources else []
        rest = self.wait(self.read_cycle({c.name for c in channels} - sources))
        order = {c.name: i for i, c in enumerate(self.reader.channels)}
        return sorted(first + rest, key=lambda r: order[r.channel])

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("the controller is already running")
        # A daemon thread, so a device that hangs can never keep the process
        # alive after close() gives up on it.
        self._thread = threading.Thread(
            # A dead tick thread would leave stop-all unanswered, so a tick
            # that fails is logged and the next one runs.
            target=self.run,
            args=(self._stop.is_set,),
            kwargs={"keep_going": True},
            name="openreactor-controller",
            daemon=True,
        )
        self._thread.start()

    def _shutdown(self) -> list[Event]:
        """Cancel everything outstanding and send stop-all, nothing else: a
        half-finished calibration must not send its next command."""
        self._auto_read = False
        self._drain_commands()  # an already queued stop-all still runs
        closed = ControllerClosed("the controller closed")
        for job in self._jobs:
            job.future.set_exception(closed)
        for request in self._cycles:
            request.future.set_exception(closed)
        self._jobs.clear()
        self._cycles.clear()
        return self._stop_all("system")

    def close(self, timeout_s: float = 5.0) -> list[Event]:
        """Send stop-all and stop ticking. Call before the bus is closed.
        Calling it again does nothing."""
        if self._closed:
            return []
        self._closed = True
        if self._thread is None or not self._thread.is_alive():
            self._thread = None
            return self._shutdown()
        future: Future[list[Event]] = Future()
        self._commands.put(lambda: future.set_result(self._shutdown()))
        try:
            return future.result(timeout=timeout_s)
        finally:
            self._stop.set()
            self._thread.join(timeout=timeout_s)
