"""RLHT heater slices over CRUMBS: start-up, the safe state, and the poll.

A slice runs its own heater PID; openreactor sets it up safely, keeps its
command watchdog fed, and reads its state. Frames go through crumbs-i2c, and
payloads through the bread-crumbs-contracts codec.

Start-up, in order, before the controller ticks (#24):

1. Version: the reply must come from an RLHT (the reply's type is checked),
   built with CRUMBS 0.12.0 or later, with a compatible RLHT module version.
2. Capabilities.
3. The safe state.
4. The watchdog, if the slice has the capability: armed with the
   controller's timeout and confirmed with GET_WATCHDOG. A slice without it
   is read-only unless its config sets ``allow_unprotected``.
5. What the config sets, each only if the slice can take it: closed-loop
   mode, thermocouple select, periods, gains.

The safe state is always SET_SETPOINTS(0, 0) and then SET_OPEN_DUTY(0, 0),
both sent even if the first fails. It never switches the slice to open loop.

The poll is GET_STATE every ``slice_poll_s``, split across two controller
ticks like an EZO read: SET_REPLY on one, the read on the next, so a tick
never waits on the bus. The firmware feeds its watchdog when it builds a
reply, so the poll is also the keep-alive, and a poll whose read fails does
not feed it.
"""

from __future__ import annotations

import errno
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
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
from crumbs_i2c import QUERY_DELAY_S, Bus, Controller, CrumbsError, LinuxBus, Message

from openreactor.config import ChannelConfig, DeviceConfig
from openreactor.controller import Event
from openreactor.ezo import Outcome, Result, Value

#: Attempts per poll: a failed read (or SET_REPLY) is retried twice (#24).
READS_PER_POLL = 3
#: Polls that fail in a row before the slice is reported unreachable.
UNREACHABLE_AFTER = 3
#: A GET_WATCHDOG check after every this many state polls.
WATCHDOG_EVERY = 5


class SliceError(Exception):
    """A slice that cannot be used: it did not answer, is not an RLHT, is
    too old, or would not arm its watchdog."""


class SlicePort(Protocol):
    """One slice on the bus. ``stage`` asks for a reply (SET_REPLY);
    ``read`` reads it and checks it is the reply asked for. ``query`` does
    both with a pause, retrying a corrupt reply or a device that did not
    answer; it is for start-up, which may wait."""

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
        self._crumbs.send(self.address, Message(0x00, 0xFE, bytes((opcode,))))

    def read(self, opcode: int) -> bytes:
        reply = self._crumbs.read_expect(self.address, type_id=self.type_id, opcode=opcode)
        return reply.data

    def query(self, opcode: int) -> bytes:
        return self._crumbs.query(self.address, type_id=self.type_id, opcode=opcode).data


class RlhtSlice:
    """An RLHT slice: an actuator for stop-all, and a source of readings."""

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
        # Read-only: unprotected and not allowed to be; never commanded
        # beyond the safe state.
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
        self._trip_count = 0
        self._last_state: RlhtStateResult | None = None
        self.unreachable = False
        self.estop = False
        # What openreactor wants the setpoints to be, in deci-degrees; set by
        # the operator, sent again after a trip or a reboot.
        self.setpoints_deci = [0, 0]

    # Start-up: blocking, before the controller ticks.

    def _query(self, opcode: int) -> bytes:
        return self.port.query(opcode)

    def start(self) -> list[Event]:
        """Check, make safe, arm and configure the slice. Raises SliceError
        if it cannot be used."""
        try:
            version = bread_parse_version(self._query(BREAD_OP_GET_VERSION))
        except CrumbsError as e:
            raise SliceError(f"no RLHT version reply: {e}") from e
        # It answered as an RLHT: from here on it is in stop-all, even if a
        # later step fails.
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
        caps = bread_caps_parse_payload(self._query(BREAD_OP_GET_CAPS))
        self.caps = caps

        self.send_safe()

        if caps.flags & RLHT_CAP_CMD_WATCHDOG:
            self.port.send(BREAD_OP_SET_WATCHDOG, rlht_send_set_watchdog(self.watchdog_timeout_ms))
            watchdog = bread_watchdog_parse_payload(self._query(BREAD_OP_GET_WATCHDOG))
            self._trip_count = watchdog.trip_count
            if not watchdog.armed or watchdog.timeout_ms != self.watchdog_timeout_ms:
                raise SliceError(
                    f"watchdog did not arm: armed={watchdog.armed}, "
                    f"timeout {watchdog.timeout_ms} ms, asked for {self.watchdog_timeout_ms} ms"
                )
            self.protected = True
        else:
            self.read_only = not self.device.allow_unprotected

        self._last_state = rlht_parse_state_payload(self._query(RLHT_OP_GET_STATE))
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
            Event(
                self._wall(),
                "system",
                "slice-start",
                device=self.name,
                details=(
                    f"RLHT {module}, CRUMBS {_crumbs_version(version.crumbs_ver)}, "
                    f"caps 0x{caps.flags:08X}; {protection}"
                ),
            )
        ]

    def _configure(self, state: RlhtStateResult, *, setpoints: bool = False) -> None:
        """In #24's order: closed-loop mode, the desired setpoints (with
        ``setpoints``, for a re-assert; zero while an e-stop is held), periods,
        thermocouple select, gains. Each only if the slice can take it, and
        periods and gains only if the config sets them. An output with no
        channel keeps the thermocouple and period the slice has; gains go only
        when the config sets both outputs'."""
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
        gains = [by_output[o] for o in (1, 2) if o in by_output]
        if (
            flags & RLHT_CAP_PID_TUNING
            and len(gains) == 2
            and all(ch.kp is not None and ch.ki is not None and ch.kd is not None for ch in gains)
        ):
            # Both outputs' gains go in one command and cannot be read back,
            # so they are sent only when the config gives all six: never a
            # filled-in value (check-config refuses anything less).
            x10 = [round(g * 10) for ch in gains for g in (ch.kp, ch.ki, ch.kd) if g is not None]
            self.port.send(RLHT_OP_SET_PID, rlht_send_set_pid_x10(*x10))

    # Stop-all

    def send_safe(self) -> None:
        """Setpoints to zero, then open-loop duty to zero: both, always, even
        if the first fails or is interrupted. Open-loop duty does nothing in
        closed loop; it stops a slice someone left in open loop. The desired
        setpoints go to zero too, so no later re-assert restarts heating."""
        self.setpoints_deci = [0, 0]
        try:
            self.port.send(RLHT_OP_SET_SETPOINTS, rlht_send_set_setpoints(0, 0))
        finally:
            self.port.send(RLHT_OP_SET_OPEN_DUTY, rlht_send_set_open_duty(0, 0))

    # The poll: called each controller tick, never waits.
    #
    # Each poll slot runs one cycle: GET_STATE, and after every fifth good
    # state poll (or when a check is owed) GET_WATCHDOG on the following
    # tick. So a watchdog check never displaces a state poll, and any
    # re-assert comes right after a GET_STATE that showed whether the slice
    # is held by its e-stop. A failure ends the cycle; the next one waits for
    # its slot.

    def advance(self, now: float) -> list[Result | Event]:
        """Do whatever is due at ``now``: stage a query, or read the one
        staged a tick ago. Returns readings and supervision events."""
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
        """A failed stage or read: retried within the cycle, twice (#24);
        after that the cycle ends, and the next waits for its slot."""
        self._attempts += 1
        if self._attempts < READS_PER_POLL:
            self._staged = False
            return self._stage(now) if isinstance(e, CrumbsError) else []
        phase, self._phase = self._phase, None
        if phase == "watchdog":
            # The slice answered GET_STATE this cycle: it is reachable. The
            # check is owed, after the next cycle's state poll.
            self._check_owed = True
            return []
        return self._poll_failed(e)

    def _poll_failed(self, e: BaseException) -> list[Result | Event]:
        self._failed_polls += 1
        out: list[Result | Event] = list(self._failed(e))
        if self._failed_polls == UNREACHABLE_AFTER and not self.unreachable:
            self.unreachable = True
            out.append(
                Event(
                    self._wall(),
                    "system",
                    "slice-unreachable",
                    device=self.name,
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
        # It may have restarted while it did not answer: check it now.
        self._check_owed = self.protected
        return [Event(self._wall(), "system", "slice-reachable", device=self.name)]

    def _estop(self, state: RlhtStateResult) -> list[Result | Event]:
        held = bool(state.flags & RLHT_FLAG_ESTOP)
        if held and not self.estop:
            self.estop = True
            # Nothing resumes after an e-stop until the operator says so;
            # the stop-all this triggers clears the setpoints too.
            self.setpoints_deci = [0, 0]
            return [
                Event(
                    self._wall(),
                    "slice",
                    "e-stop",
                    device=self.name,
                    details="e-stop pressed on the slice",
                    result="held",
                )
            ]
        if held:
            self._send_safe_quietly()
            return []
        if self.estop:
            self.estop = False
            return [
                Event(
                    self._wall(),
                    "slice",
                    "e-stop",
                    device=self.name,
                    details="e-stop released; setpoints stay at 0 until set again",
                    result="released",
                )
            ]
        return []

    def _supervise(self, watchdog: BreadWatchdogResult) -> list[Result | Event]:
        """A trip or a reboot means the slice dropped what it was told:
        re-assert it once, and check it took in the next cycle."""
        baseline = self._trip_count
        self._trip_count = watchdog.trip_count
        confirming, self._confirming = self._confirming, False
        if not watchdog.armed:
            what = "slice-reboot"
            details = f"watchdog disarmed (trips {watchdog.trip_count})"
        elif watchdog.tripped or watchdog.trip_count != baseline:
            # A count below the baseline with the watchdog armed is the u8
            # counter wrapping, or a build that arms at boot: either way the
            # slice dropped its state.
            what = "slice-trip"
            details = (
                f"watchdog tripped (tripped={watchdog.tripped}, "
                f"trips {baseline} -> {watchdog.trip_count})"
            )
        else:
            return []
        if confirming:
            # The last re-assert did not take. Say so, and wait for the next
            # scheduled check rather than re-asserting at once.
            return [
                Event(
                    self._wall(),
                    "system",
                    what,
                    device=self.name,
                    details=details,
                    result="error: the re-assert did not take",
                )
            ]
        try:
            self._reassert()
        except (CrumbsError, OSError, ValueError) as e:
            # Keep the old baseline, so the next check sees the trip again
            # and the re-assert is retried.
            self._trip_count = baseline
            self._check_owed = True
            failure = f"error: re-assert failed: {_describe(e)}"
            return [
                Event(
                    self._wall(), "system", what, device=self.name, details=details, result=failure
                )
            ]
        self._confirming = True
        self._check_owed = True
        return [
            Event(
                self._wall(),
                "system",
                what,
                device=self.name,
                details=details,
                result="re-asserted",
            )
        ]

    def _reassert(self) -> None:
        """The desired state, in the order #24 gives: mode, setpoints,
        periods, thermocouples, gains, then the watchdog armed again. Only
        ever right after a GET_STATE, so a held e-stop is known."""
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
    """The config's value for an output, or what the slice has now."""
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
    """Open each RLHT's bus (one per path, shared) and start each slice.
    Yields every slice that answered as an RLHT, for stop-all, whether or
    not its start-up finished (``started`` says which, for polling), and the
    start-up events and problems. The buses close on the way out; send
    stop-all before that."""
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
                        # In stop-all whether or not start-up finished.
                        slices.append(rlht)
            except (SliceError, CrumbsError, OSError, ValueError) as e:
                reports.append(Result(d.name, Outcome.ERROR, detail=_describe(e)))
        yield slices, reports
    finally:
        for bus in buses.values():
            close = getattr(bus, "close", None)
            if close is not None:
                close()
