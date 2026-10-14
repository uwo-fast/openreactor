"""Start, supervise and poll RLHT heater slices over CRUMBS.

Each slice runs its own heater PID. This module starts a slice safely,
keeps its command watchdog fed by polling it, re-asserts its settings after
a watchdog trip, and turns its state into readings. Frames go through
crumbs-i2c, and payloads through the bread-crumbs-contracts codec.
"""

from __future__ import annotations

import errno
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Protocol

from bread_crumbs_contracts.bread_caps import (
    BREAD_OP_GET_CAPS,
    BreadCapsResult,
    bread_caps_parse_payload,
    bread_is_valid_i16,
)
from bread_crumbs_contracts.bread_version_helpers import (
    BREAD_OP_GET_VERSION,
    bread_check_crumbs_compat,
    bread_check_module_compat,
    bread_parse_version,
)
from bread_crumbs_contracts.bread_watchdog import (
    BREAD_OP_GET_WATCHDOG,
    BREAD_OP_SET_WATCHDOG,
    BreadWatchdogResult,
    bread_watchdog_parse_payload,
)
from bread_crumbs_contracts.rlht_ops import (
    RLHT_CAP_CMD_WATCHDOG,
    RLHT_CAP_MODE_CONTROL,
    RLHT_CAP_PERIOD_CONTROL,
    RLHT_CAP_PID_TUNING,
    RLHT_CAP_TC_SELECT,
    RLHT_FLAG_ESTOP,
    RLHT_MODE_CLOSED_LOOP,
    RLHT_MODE_OPEN_LOOP,
    RLHT_MODULE_VER_MAJOR,
    RLHT_MODULE_VER_MINOR,
    RLHT_OP_GET_STATE,
    RLHT_OP_SET_MODE,
    RLHT_OP_SET_OPEN_DUTY,
    RLHT_OP_SET_PERIODS,
    RLHT_OP_SET_PID,
    RLHT_OP_SET_SETPOINTS,
    RLHT_OP_SET_TC_SELECT,
    RLHT_TYPE_ID,
    RlhtStateResult,
    rlht_parse_state_payload,
    rlht_send_set_mode,
    rlht_send_set_open_duty,
    rlht_send_set_periods,
    rlht_send_set_pid_x10,
    rlht_send_set_setpoints,
    rlht_send_set_tc_select,
    rlht_send_set_watchdog,
)
from crumbs_i2c import (
    QUERY_DELAY_S,
    SET_REPLY,
    TYPE_ID_ANY,
    Bus,
    Controller,
    CrumbsError,
    LinuxBus,
    Message,
)

from openreactor.config import ChannelConfig, DeviceConfig
from openreactor.controller import Event
from openreactor.ezo import Outcome, Result, Value

#: Attempts at each SET_REPLY or read in a poll cycle before the cycle ends.
READS_PER_POLL = 3
#: Failures in a row before giving up: state polls before the slice is
#: reported unreachable, and watchdog checks before they are reported as
#: failing and are no longer retried early.
UNREACHABLE_AFTER = 3
#: Good state polls between routine GET_WATCHDOG checks.
WATCHDOG_EVERY = 5


class SliceError(Exception):
    """Raised when a slice cannot be used.

    It did not answer, is not an RLHT, runs firmware that is too old, or
    would not arm its watchdog.
    """


class SendFailed(SliceError):
    """Raised when a command may or may not have reached the slice."""


@dataclass(frozen=True)
class OutputStatus:
    """One heater output as the slice last reported it."""

    output: int
    channel: str | None  # the config's channel on this output, if any
    tc: int
    temperature_c: float | None  # None when there is no thermocouple reading
    setpoint_c: float | None
    on_ms: int
    period_ms: int


@dataclass(frozen=True)
class WatchdogStatus:
    armed: bool
    timeout_ms: int
    tripped: bool
    trip_count: int


@dataclass(frozen=True)
class SliceStatus:
    """One slice as the running controller last saw it."""

    name: str
    address: int
    state: str  # "ok", or why it is out of use
    version: str | None = None  # "RLHT 1.0.0, CRUMBS 0.15.0"
    caps: int | None = None
    mode: str | None = None  # "closed loop" or "open loop"
    estop: bool | None = None
    outputs: tuple[OutputStatus, ...] = ()
    watchdog: WatchdogStatus | None = None
    # The setpoints the controller wants, in °C; None for a slice it does
    # not drive or could not report on.
    desired_c: tuple[float, float] | None = None


def _outputs(device: DeviceConfig, state: RlhtStateResult) -> tuple[OutputStatus, ...]:
    names = {ch.output: ch.name for ch in device.channels}

    def degrees(deci: int) -> float | None:
        return deci / 10 if bread_is_valid_i16(deci) else None

    return (
        OutputStatus(
            1,
            names.get(1),
            state.tc1,
            degrees(state.t1_deci_c),
            degrees(state.sp1_deci_c),
            state.on1_ms,
            state.period1_ms,
        ),
        OutputStatus(
            2,
            names.get(2),
            state.tc2,
            degrees(state.t2_deci_c),
            degrees(state.sp2_deci_c),
            state.on2_ms,
            state.period2_ms,
        ),
    )


def _mode(state: RlhtStateResult) -> str:
    if state.mode == RLHT_MODE_CLOSED_LOOP:
        return "closed loop"
    if state.mode == RLHT_MODE_OPEN_LOOP:
        return "open loop"
    return f"mode {state.mode}"


def _watchdog(w: BreadWatchdogResult) -> WatchdogStatus:
    return WatchdogStatus(bool(w.armed), w.timeout_ms, bool(w.tripped), w.trip_count)


class SliceBusy(Exception):
    """Raised when the slice cannot take a setpoint now.

    Its e-stop is held, it is read-only, or a check of setpoints that
    drifted from openreactor's is still pending.
    """


class SlicePort(Protocol):
    """Send commands to one slice on the bus and read its replies.

    ``stage`` asks for a reply (SET_REPLY), and ``read`` reads it and checks
    it is the reply asked for. ``query`` does both with a pause and retries
    a corrupt reply or a device that did not answer; start-up uses it
    because it may wait.
    """

    def send(self, opcode: int, payload: bytes) -> None: ...

    def stage(self, opcode: int) -> None: ...

    def read(self, opcode: int) -> bytes: ...

    def query(self, opcode: int) -> bytes: ...


class CrumbsPort:
    def __init__(
        self,
        bus: Bus,
        address: int,
        type_id: int = RLHT_TYPE_ID,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._crumbs = Controller(bus, sleep=sleep)
        self.address = address
        self.type_id = type_id

    def send(self, opcode: int, payload: bytes) -> None:
        self._crumbs.send(self.address, Message(self.type_id, opcode, payload))

    def stage(self, opcode: int) -> None:
        self._crumbs.send(self.address, Message(TYPE_ID_ANY, SET_REPLY, bytes((opcode,))))

    def read(self, opcode: int) -> bytes:
        reply = self._crumbs.read_expect(self.address, type_id=self.type_id, opcode=opcode)
        return reply.data

    def query(self, opcode: int) -> bytes:
        return self._crumbs.query(self.address, type_id=self.type_id, opcode=opcode).data


class RlhtSlice:
    """One RLHT slice, which stop-all commands and the poll reads."""

    def __init__(
        self,
        device: DeviceConfig,
        port: SlicePort,
        *,
        watchdog_timeout_ms: int,
        poll_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], float] = time.time,
    ):
        self.device = device
        self.name = device.name
        self.port = port
        self.watchdog_timeout_ms = watchdog_timeout_ms
        self.poll_s = poll_s
        self._clock = clock
        self._sleep = sleep
        self._wall = wall
        self.caps: BreadCapsResult | None = None
        self.identified = False
        self.started = False
        self.protected = False
        # Set when the slice has no command watchdog and its config does not
        # allow that. A read-only slice is sent the safe state and nothing else.
        self.read_only = False
        self._next_poll = 0.0
        self._staged_at = 0.0
        self._phase: str | None = None
        self._staged = False
        self._attempts = 0
        self._polls = 0
        self._failed_polls = 0
        self._check_owed = False
        self._confirming = False
        self._failed_checks = 0
        # The desired setpoints the last GET_STATE disagreed with, if it did.
        self._drift_against: list[int] | None = None
        # The last drop's result, so a drop repeated every poll is reported
        # only when its result changes.
        self._drop_result: str | None = None
        self._trip_count = 0
        self._last_state: RlhtStateResult | None = None
        self._last_watchdog: BreadWatchdogResult | None = None
        self._version: str | None = None
        self.unreachable = False
        self.estop = False
        # The setpoints openreactor wants, in tenths of a degree. The operator
        # sets them, and a re-assert sends them again after a watchdog trip.
        self.setpoints_deci = [0, 0]

    def _event(
        self, kind: str, details: str = "", result: str = "ok", source: str = "system"
    ) -> Event:
        """Return a run-log event about this slice, stamped with the wall clock."""
        return Event(self._wall(), source, kind, device=self.name, details=details, result=result)

    # Start-up blocks, and runs before the controller starts ticking.

    def start(self) -> list[Event]:
        """Check, make safe, arm and configure the slice.

        The steps run in this order. GET_VERSION must come from an RLHT
        built with CRUMBS 0.12.0 or later and a compatible RLHT module
        version. GET_CAPS follows, then the safe state. A slice with the
        watchdog capability has it armed with the controller's timeout and
        confirmed with GET_WATCHDOG; one without it is read-only, and left
        unconfigured, unless its config sets ``allow_unprotected``. Last,
        ``_configure`` sends what the config sets.

        Raise SliceError if the slice cannot be used.
        """
        try:
            version = bread_parse_version(self.port.query(BREAD_OP_GET_VERSION))
        except CrumbsError as e:
            raise SliceError(f"no RLHT version reply: {e}") from e
        # It answered as an RLHT, so stop-all includes it from here on, even
        # if a later step fails.
        self.identified = True
        if bread_check_crumbs_compat(version.crumbs_ver) != 0:
            raise SliceError(
                f"built with CRUMBS {_crumbs_version(version.crumbs_ver)}; "
                "0.12.0 or later is needed"
            )
        module = f"{version.mod_major}.{version.mod_minor}.{version.mod_patch}"
        if (
            bread_check_module_compat(
                version.mod_major, version.mod_minor, RLHT_MODULE_VER_MAJOR, RLHT_MODULE_VER_MINOR
            )
            != 0
        ):
            raise SliceError(
                f"runs RLHT {module}; openreactor needs "
                f"{RLHT_MODULE_VER_MAJOR}.{RLHT_MODULE_VER_MINOR} or a later minor"
            )
        self._version = f"RLHT {module}, CRUMBS {_crumbs_version(version.crumbs_ver)}"
        caps = bread_caps_parse_payload(self.port.query(BREAD_OP_GET_CAPS))
        self.caps = caps

        self.send_safe()

        if caps.flags & RLHT_CAP_CMD_WATCHDOG:
            self.port.send(BREAD_OP_SET_WATCHDOG, rlht_send_set_watchdog(self.watchdog_timeout_ms))
            watchdog = bread_watchdog_parse_payload(self.port.query(BREAD_OP_GET_WATCHDOG))
            self._last_watchdog = watchdog
            self._trip_count = watchdog.trip_count
            if not watchdog.armed or watchdog.timeout_ms != self.watchdog_timeout_ms:
                raise SliceError(
                    f"watchdog did not arm: armed={watchdog.armed}, "
                    f"timeout {watchdog.timeout_ms} ms, asked for {self.watchdog_timeout_ms} ms"
                )
            self.protected = True
        else:
            self.read_only = not self.device.allow_unprotected

        self._last_state = rlht_parse_state_payload(self.port.query(RLHT_OP_GET_STATE))
        if not self.read_only:
            self._configure(self._last_state)

        self._next_poll = self._clock()
        self.started = True
        protection = (
            f"watchdog armed at {self.watchdog_timeout_ms} ms"
            if self.protected
            else "no watchdog: read-only"
            if self.read_only
            else "no watchdog: allow_unprotected"
        )
        return [
            self._event(
                "slice-start", details=f"{self._version}, caps 0x{caps.flags:08X}; {protection}"
            )
        ]

    def _configure(self, state: RlhtStateResult, *, setpoints: bool = False) -> None:
        """Send the config's settings, each only if the slice can take it.

        The order is closed-loop mode, the desired setpoints (only with
        ``setpoints``, for a re-assert, and zero while an e-stop is held),
        periods, thermocouple select, then gains. Periods go only if the
        config sets one, and gains only if it sets them. An output with no
        channel keeps the slice's thermocouple and period, and gets the
        other output's gains.
        """
        flags = self.caps.flags if self.caps else 0
        by_output = {ch.output: ch for ch in self.device.channels}

        if flags & RLHT_CAP_MODE_CONTROL:
            self.port.send(RLHT_OP_SET_MODE, rlht_send_set_mode(RLHT_MODE_CLOSED_LOOP))
        if setpoints:
            sp1, sp2 = (0, 0) if self.estop else self.setpoints_deci
            self.port.send(RLHT_OP_SET_SETPOINTS, rlht_send_set_setpoints(sp1, sp2))
        if flags & RLHT_CAP_PERIOD_CONTROL and any(ch.period_ms for ch in by_output.values()):
            p1 = _setting(by_output, 1, "period_ms", state.period1_ms)
            p2 = _setting(by_output, 2, "period_ms", state.period2_ms)
            self.port.send(RLHT_OP_SET_PERIODS, rlht_send_set_periods(p1, p2))
        if flags & RLHT_CAP_TC_SELECT:
            tc1 = _setting(by_output, 1, "tc", state.tc1)
            tc2 = _setting(by_output, 2, "tc", state.tc2)
            self.port.send(RLHT_OP_SET_TC_SELECT, rlht_send_set_tc_select(tc1, tc2))
        if (
            flags & RLHT_CAP_PID_TUNING
            and by_output
            and all(
                ch.kp is not None and ch.ki is not None and ch.kd is not None
                for ch in by_output.values()
            )
        ):
            # SET_PID carries both outputs' gains, and the slice cannot report
            # its current ones. An output with no channel gets the other
            # output's gains rather than 0: with ki = 0, firmware without
            # feastorg/Slice_RLHT#13 keeps a stale integral, where a ki above
            # 0 lets it unwind to off at a setpoint of 0. check-config refuses
            # gains on some channels and not others.
            pair = [by_output.get(o) or by_output[3 - o] for o in (1, 2)]
            x10 = [round(g * 10) for ch in pair for g in (ch.kp, ch.ki, ch.kd) if g is not None]
            self.port.send(RLHT_OP_SET_PID, rlht_send_set_pid_x10(*x10))

    # Stop-all

    def send_safe(self) -> None:
        """Send the safe state: zero setpoints, then zero open-loop duty.

        The second is sent even if the first fails or is interrupted.
        Open-loop duty does nothing in closed loop, but stops a slice someone
        left in open loop; this never switches the slice to open loop. The
        desired setpoints go to zero too, so no later re-assert restarts
        heating.
        """
        self.setpoints_deci = [0, 0]
        try:
            self.port.send(RLHT_OP_SET_SETPOINTS, rlht_send_set_setpoints(0, 0))
        finally:
            self.port.send(RLHT_OP_SET_OPEN_DUTY, rlht_send_set_open_duty(0, 0))

    def status(self, why: str | None = None) -> SliceStatus:
        """Return the slice's status as of the last poll.

        Call it on the controller's thread. ``why`` is the start-up problem
        of a slice that did not start.
        """
        if why is not None:
            state = why
        elif not self.started:
            state = "did not finish start-up"
        elif self.unreachable:
            state = f"unreachable: {UNREACHABLE_AFTER} polls failed in a row"
        elif self.estop:
            state = "e-stop held on the slice"
        elif self.read_only:
            state = "read-only: the slice has no command watchdog"
        else:
            state = "ok"
        last = self._last_state
        return SliceStatus(
            self.name,
            self.device.address,
            state,
            version=self._version,
            caps=self.caps.flags if self.caps else None,
            mode=_mode(last) if last else None,
            estop=bool(last.flags & RLHT_FLAG_ESTOP) if last else None,
            outputs=_outputs(self.device, last) if last else (),
            watchdog=_watchdog(self._last_watchdog) if self._last_watchdog else None,
            desired_c=(self.setpoints_deci[0] / 10, self.setpoints_deci[1] / 10),
        )

    def set_setpoint(self, output: int, deci: int) -> None:
        """Send an operator's setpoint for one output, in tenths of a degree.

        Call it on the controller's thread. SET_SETPOINTS carries both
        outputs' setpoints, so the other output's is sent again too. First,
        the slice is read to confirm it still runs what openreactor last
        sent, with its watchdog armed: after a reboot that no poll has seen
        yet, such as a power-cut e-stop just released, sending the other
        output's old setpoint would restart that heater with the watchdog
        disarmed. The desired setpoints, which a re-assert sends again,
        change only once the send succeeds.
        """
        if not self.started:
            raise SliceError(f"{self.name} did not finish start-up")
        if self.read_only:
            raise SliceBusy(f"{self.name} is read-only: the slice has no command watchdog")
        if self.estop:
            raise SliceBusy(
                f"the e-stop on {self.name} is held; release it, then set the setpoint again"
            )
        if self.unreachable:
            raise SliceError(f"{self.name} is unreachable: {UNREACHABLE_AFTER} polls failed")
        if self._drift_against is not None:
            # Until the pending check explains the drift, a new setpoint would
            # be judged against a stale reading and would carry the other
            # output's old setpoint with it.
            raise SliceBusy(
                f"{self.name} runs other setpoints than wanted, and is being checked; "
                "set it again in a moment"
            )
        self._confirm_unchanged()
        wanted = list(self.setpoints_deci)
        wanted[output - 1] = deci
        try:
            self.port.send(RLHT_OP_SET_SETPOINTS, rlht_send_set_setpoints(wanted[0], wanted[1]))
        except (CrumbsError, OSError) as e:
            raise SendFailed(f"{self.name}: {_describe(e)}") from e
        self.setpoints_deci = wanted

    def _confirm_unchanged(self) -> None:
        """Read the slice now, and raise unless it is as openreactor left it.

        Its watchdog must have the controller's timeout (0 means disarmed,
        as after a reboot), not be tripped and have the trip count last
        seen, and its state must show no e-stop and the setpoints
        openreactor last sent. Otherwise a check is owed, which the next
        poll runs, and the caller waits for it.
        """
        # Each query stages its own reply over the one the poll staged. The
        # slice builds a reply when it is read, so ending with the query the
        # poll staged leaves it reading a fresh reply of the kind it expects,
        # however often setpoints are sent.
        watchdog_last = self._staged and self._phase == "watchdog"
        watchdog: BreadWatchdogResult | None = None
        try:
            if self.protected and not watchdog_last:
                watchdog = bread_watchdog_parse_payload(self.port.query(BREAD_OP_GET_WATCHDOG))
            state = rlht_parse_state_payload(self.port.query(RLHT_OP_GET_STATE))
            if watchdog_last:
                watchdog = bread_watchdog_parse_payload(self.port.query(BREAD_OP_GET_WATCHDOG))
        except (CrumbsError, OSError, ValueError) as e:
            self._staged = False  # what the slice has staged is unknown: stage again
            raise SliceError(f"{self.name}: could not read the slice: {_describe(e)}") from e
        changed = bool(state.flags & RLHT_FLAG_ESTOP) or self._drift(state)
        if watchdog is not None:
            changed = changed or (
                watchdog.timeout_ms != self.watchdog_timeout_ms
                or watchdog.tripped
                or watchdog.trip_count != self._trip_count
            )
        if changed:
            self._check_owed = True
            raise SliceBusy(
                f"{self.name} changed since it was last checked, and is being checked; "
                "set the setpoint again in a moment"
            )

    # The poll runs on every controller tick and never waits: it stages a
    # query (SET_REPLY) on one tick and reads it on a later one. Each poll
    # slot runs one cycle: GET_STATE, then GET_WATCHDOG on the next tick when
    # a check is owed or WATCHDOG_EVERY good state polls have passed. So a
    # watchdog check never displaces a state poll, and any re-assert comes
    # right after a GET_STATE that showed whether the e-stop is held. A
    # failure ends the cycle; the next waits for its slot. The firmware feeds
    # its watchdog when it builds a reply, so the poll is also the
    # keep-alive, and a poll whose read fails does not feed it.

    def advance(self, now: float) -> list[Result | Event]:
        """Stage or read whatever is due at ``now``; return readings and events."""
        if self._phase is None:
            if now < self._next_poll:
                return []
            self._next_poll += self.poll_s
            if self._next_poll <= now:  # fell behind: skip to the next slot
                self._next_poll = now + self.poll_s
            self._begin("state")
        if not self._staged:
            return self._stage(now)
        if now < self._staged_at + QUERY_DELAY_S:
            return []
        opcode = BREAD_OP_GET_WATCHDOG if self._phase == "watchdog" else RLHT_OP_GET_STATE
        try:
            payload = self.port.read(opcode)
            parsed: BreadWatchdogResult | RlhtStateResult = (
                bread_watchdog_parse_payload(payload)
                if self._phase == "watchdog"
                else rlht_parse_state_payload(payload)
            )
        except (CrumbsError, OSError, ValueError) as e:
            return self._attempt_failed(e, now)
        if isinstance(parsed, BreadWatchdogResult):
            self._phase = None
            return self._supervise(parsed)
        state = parsed
        self._last_state = state
        out: list[Result | Event] = self._answered()
        out += self._estop(state)
        out += self._readings(state)
        out += self._drifted(state)
        self._polls += 1
        if self.protected and (self._check_owed or self._polls % WATCHDOG_EVERY == 0):
            self._check_owed = False
            self._begin("watchdog")  # staged on the next tick
        else:
            self._phase = None
        return out

    def _begin(self, phase: str) -> None:
        self._phase = phase
        self._attempts = 0
        self._staged = False

    def _stage(self, now: float) -> list[Result | Event]:
        opcode = BREAD_OP_GET_WATCHDOG if self._phase == "watchdog" else RLHT_OP_GET_STATE
        try:
            self.port.stage(opcode)
        except (CrumbsError, OSError) as e:
            return self._attempt_failed(e, now)
        self._staged = True
        self._staged_at = now
        return []

    def _attempt_failed(self, e: BaseException, now: float) -> list[Result | Event]:
        """Retry a failed stage or read, up to READS_PER_POLL tries, then end the cycle."""
        self._attempts += 1
        if self._attempts < READS_PER_POLL:
            self._staged = False
            return self._stage(now) if isinstance(e, CrumbsError) else []
        phase, self._phase = self._phase, None
        if phase == "watchdog":
            # The slice answered GET_STATE this cycle, so it is reachable.
            # Retry the check after the next state poll until it has failed
            # UNREACHABLE_AFTER times in a row; then report that once and
            # leave the check to its usual slot.
            self._failed_checks += 1
            if self._failed_checks < UNREACHABLE_AFTER:
                self._check_owed = True
                return []
            if self._failed_checks > UNREACHABLE_AFTER:
                return []
            # Drop any pending confirm; the next check that answers is fresh.
            self._confirming = False
            return [
                self._event(
                    "slice-unchecked",
                    details=(
                        f"{UNREACHABLE_AFTER} watchdog checks failed in a row: "
                        f"{_describe(e)}; trips and reboots go unseen until one answers"
                    ),
                    result="error",
                )
            ]
        return self._poll_failed(e)

    def _poll_failed(self, e: BaseException) -> list[Result | Event]:
        self._failed_polls += 1
        out: list[Result | Event] = list(self._failed(e))
        if self._failed_polls == UNREACHABLE_AFTER and not self.unreachable:
            self.unreachable = True
            out.append(
                self._event(
                    "slice-unreachable",
                    details=f"{UNREACHABLE_AFTER} polls failed: {_describe(e)}",
                    result="error",
                )
            )
        return out

    def _answered(self) -> list[Result | Event]:
        self._failed_polls = 0
        if not self.unreachable:
            return []
        self.unreachable = False
        # It may have restarted while it did not answer: check it now, as a
        # fresh check, whatever was pending before.
        self._check_owed = self.protected
        self._confirming = False
        return [self._event("slice-reachable")]

    def _estop(self, state: RlhtStateResult) -> list[Result | Event]:
        held = bool(state.flags & RLHT_FLAG_ESTOP)
        if held and not self.estop:
            self.estop = True
            # Nothing resumes after an e-stop until the operator sets a
            # setpoint again; the stop-all this event triggers clears them too.
            self.setpoints_deci = [0, 0]
            return [
                self._event(
                    "e-stop", details="e-stop pressed on the slice", result="held", source="slice"
                )
            ]
        if held:
            self._send_safe_quietly()
            return []
        if self.estop:
            self.estop = False
            return [
                self._event(
                    "e-stop",
                    details="e-stop released; setpoints stay at 0 until set again",
                    result="released",
                    source="slice",
                )
            ]
        return []

    def _drift(self, state: RlhtStateResult) -> bool:
        """Return whether the slice runs other setpoints than openreactor wants.

        A held e-stop is handled on its own, and a read-only slice is not
        openreactor's to set, so neither counts.
        """
        if self.estop or self.read_only:
            return False
        return [state.sp1_deci_c, state.sp2_deci_c] != self.setpoints_deci

    def _drifted(self, state: RlhtStateResult) -> list[Result | Event]:
        """Owe a watchdog check when the setpoints drifted, or drop them if no check can run.

        A trip or a reboot zeroes the slice's setpoints, and the check finds
        either and re-asserts; if it finds neither, ``_supervise`` drops them.
        """
        if not self._drift(state):
            self._drift_against = None
            self._drop_result = None
            return []
        if self.protected and self._failed_checks < UNREACHABLE_AFTER:
            self._drift_against = list(self.setpoints_deci)
            self._check_owed = True
            return []
        self._drift_against = None  # no check is pending
        return self._drop(state)

    def _drop(self, state: RlhtStateResult) -> list[Result | Event]:
        """Send the safe state after setpoints changed with no trip or reboot.

        The cause may be an e-stop pressed and released between two polls, a
        setpoint that reached the slice although sending it failed, or
        another controller. Nothing resumes until the operator sets a
        setpoint again.
        """
        ran = f"{state.sp1_deci_c / 10:g} and {state.sp2_deci_c / 10:g} °C"
        wanted = f"{self.setpoints_deci[0] / 10:g} and {self.setpoints_deci[1] / 10:g} °C"
        try:
            self.send_safe()  # zeroes the desired setpoints too
            result = "safe state sent"
        except (CrumbsError, OSError) as e:
            self.setpoints_deci = [0, 0]
            result = f"error: safe state not sent: {_describe(e)}"
        if result == self._drop_result:
            return []  # retried each poll, reported once per result
        self._drop_result = result
        return [
            self._event(
                "slice-setpoints-changed",
                details=(
                    f"the slice ran {ran}, not {wanted}, with no trip or reboot to explain "
                    "it; setpoints stay at 0 until set again"
                ),
                result=result,
            )
        ]

    def _supervise(self, watchdog: BreadWatchdogResult) -> list[Result | Event]:
        """Act on a watchdog check.

        A trip or a reboot means the slice dropped what it was told:
        re-assert it once, and check in the next cycle that it took. With
        neither, drop setpoints that the last GET_STATE found had drifted.
        """
        self._failed_checks = 0
        self._last_watchdog = watchdog
        against, self._drift_against = self._drift_against, None
        baseline = self._trip_count
        self._trip_count = watchdog.trip_count
        confirming, self._confirming = self._confirming, False
        if not watchdog.armed:
            what = "slice-reboot"
            details = f"watchdog disarmed (trips {watchdog.trip_count})"
            rebooted = True
        elif watchdog.timeout_ms != self.watchdog_timeout_ms:
            # Most likely a build that arms at boot, rebooted with its own
            # timeout; otherwise someone changed it at the slice. Either
            # way, treat it as a reboot.
            what = "slice-reboot"
            details = (
                f"watchdog timeout {watchdog.timeout_ms} ms, not {self.watchdog_timeout_ms} ms "
                f"(trips {watchdog.trip_count})"
            )
            rebooted = True
        elif watchdog.tripped or watchdog.trip_count != baseline:
            # A count below the baseline with the watchdog armed is the u8
            # counter wrapping, or a build that arms at boot: either way the
            # slice dropped its state.
            what = "slice-trip"
            details = (
                f"watchdog tripped (tripped={watchdog.tripped}, "
                f"trips {baseline} -> {watchdog.trip_count})"
            )
            rebooted = watchdog.trip_count < baseline
        else:
            # Drop only if the desired setpoints are still the ones GET_STATE
            # disagreed with; a setpoint set since is judged by the next one.
            last = self._last_state
            if last is None or against is None or against != self.setpoints_deci:
                return []
            return self._drop(last)
        if rebooted:
            # A slice reboots when its power is cut, for instance by an
            # e-stop wired to the backplane, so nothing resumes until the
            # operator sets a setpoint again.
            self.setpoints_deci = [0, 0]
            details += "; setpoints stay at 0 until set again"
        if confirming:
            # The last re-assert did not take. Say so, and wait for the next
            # scheduled check rather than re-asserting at once.
            return [self._event(what, details=details, result="error: the re-assert did not take")]
        try:
            self._reassert()
        except (CrumbsError, OSError, ValueError) as e:
            # Keep the old baseline, so the next check sees the trip again
            # and the re-assert is retried.
            self._trip_count = baseline
            self._check_owed = True
            failure = f"error: re-assert failed: {_describe(e)}"
            return [self._event(what, details=details, result=failure)]
        self._confirming = True
        self._check_owed = True
        return [self._event(what, details=details, result="re-asserted")]

    def _reassert(self) -> None:
        """Send the desired state again, then re-arm the watchdog.

        The settings go in ``_configure``'s order: mode, setpoints, periods,
        thermocouples, gains. This runs only right after a GET_STATE, so
        whether the e-stop is held is known.
        """
        if self.read_only or self._last_state is None:
            return
        self._configure(self._last_state, setpoints=True)
        if self.protected:
            self.port.send(BREAD_OP_SET_WATCHDOG, rlht_send_set_watchdog(self.watchdog_timeout_ms))

    def _send_safe_quietly(self) -> None:
        # A failure is retried at the next poll; the e-stop holds the relays off.
        with suppress(CrumbsError, OSError):
            self.send_safe()

    def _failed(self, e: BaseException) -> list[Result]:
        detail = _describe(e)
        return [Result(ch.name, Outcome.ERROR, detail=detail) for ch in self.device.channels]

    def _readings(self, state: RlhtStateResult) -> list[Result]:
        results: list[Result] = []
        for ch in self.device.channels:
            temperature, setpoint, on_ms, period_ms = (
                (state.t1_deci_c, state.sp1_deci_c, state.on1_ms, state.period1_ms)
                if ch.output == 1
                else (state.t2_deci_c, state.sp2_deci_c, state.on2_ms, state.period2_ms)
            )
            values: list[Value] = []
            if bread_is_valid_i16(temperature):
                values.append(Value("temperature", temperature / 10, "°C"))
            if bread_is_valid_i16(setpoint):
                values.append(Value("setpoint", setpoint / 10, "°C"))
            if period_ms:
                values.append(Value("duty", round(100 * on_ms / period_ms, 1), "%"))
            results.append(Result(ch.name, Outcome.OK, tuple(values)))
        return results


def _setting(by_output: dict[int, ChannelConfig], output: int, key: str, current: int) -> int:
    """Return the config's ``key`` for an output, or ``current`` if unset."""
    ch = by_output.get(output)
    value = getattr(ch, key) if ch is not None else None
    return value if isinstance(value, int) else current


def _crumbs_version(word: int) -> str:
    return f"{word // 10000}.{word // 100 % 100}.{word % 100}"


def _describe(e: BaseException) -> str:
    if isinstance(e, OSError) and e.errno in (errno.ENXIO, errno.EREMOTEIO):
        return "no answer on the bus"
    return str(e) or type(e).__name__


def _open_linux_bus(path: str) -> Bus:
    return LinuxBus(path)


@contextmanager
def opened_slices(
    devices: Sequence[DeviceConfig],
    *,
    watchdog_timeout_ms: int,
    poll_s: float,
    open_bus: Callable[[str], Bus] = _open_linux_bus,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wall: Callable[[], float] = time.time,
) -> Iterator[tuple[list[RlhtSlice], list[Result | Event]]]:
    """Open each RLHT's bus and start each slice.

    Slices on the same bus path share one bus. Yield every slice that
    answered as an RLHT, so stop-all covers it whether or not its start-up
    finished (``started`` says which to poll), with the start-up events and
    problems. The buses close on exit, so send stop-all before that.
    """
    buses: dict[str, Bus] = {}
    slices: list[RlhtSlice] = []
    reports: list[Result | Event] = []
    try:
        for d in devices:
            if d.kind != "rlht":
                continue
            try:
                if d.bus not in buses:
                    buses[d.bus] = open_bus(d.bus)
                rlht = RlhtSlice(
                    d,
                    CrumbsPort(buses[d.bus], d.address, sleep=sleep),
                    watchdog_timeout_ms=watchdog_timeout_ms,
                    poll_s=poll_s,
                    clock=clock,
                    sleep=sleep,
                    wall=wall,
                )
                try:
                    reports += rlht.start()
                finally:
                    if rlht.identified:
                        slices.append(rlht)
            except (SliceError, CrumbsError, OSError, ValueError) as e:
                reports.append(Result(d.name, Outcome.ERROR, detail=_describe(e)))
        yield slices, reports
    finally:
        for bus in buses.values():
            close = getattr(bus, "close", None)
            if close is not None:
                close()
