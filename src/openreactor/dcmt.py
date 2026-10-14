"""Start, supervise and poll DCMT motor slices over CRUMBS.

openreactor drives each DCMT motor open loop, at a duty from -100 % to
100 %. The slice keeps its own position and speed loops, which openreactor
does not use. This module starts a slice safely, keeps its command watchdog
fed by polling it, and turns its state into readings.

Unlike a heater, a motor never resumes on its own. After an e-stop, a
watchdog trip or a reboot, both motors stay stopped until the operator sets
a duty again. A slice whose watchdog trip latches stays held until the
operator clears the trip; openreactor never clears it unasked.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

from bread_crumbs_contracts.bread_caps import (
    BREAD_OP_GET_CAPS,
    BreadCapsResult,
    bread_caps_parse_payload,
)
from bread_crumbs_contracts.bread_version_helpers import (
    BREAD_OP_GET_VERSION,
    bread_check_crumbs_compat,
    bread_check_module_compat,
    bread_parse_version,
)
from bread_crumbs_contracts.bread_watchdog import (
    BREAD_OP_CLEAR_WATCHDOG_TRIP,
    BREAD_OP_GET_WATCHDOG,
    BREAD_OP_SET_WATCHDOG,
    BreadWatchdogResult,
    bread_watchdog_parse_payload,
)
from bread_crumbs_contracts.dcmt_ops import (
    DCMT_CAP_BRAKE_CONTROL,
    DCMT_CAP_CLEAR_WATCHDOG_TRIP,
    DCMT_CAP_CMD_WATCHDOG,
    DCMT_CAP_OPEN_LOOP_CONTROL,
    DCMT_MODE_CLOSED_POSITION,
    DCMT_MODE_CLOSED_SPEED,
    DCMT_MODE_OPEN_LOOP,
    DCMT_MODULE_VER_MAJOR,
    DCMT_MODULE_VER_MINOR,
    DCMT_OP_GET_STATE,
    DCMT_OP_SET_BRAKE,
    DCMT_OP_SET_MODE,
    DCMT_OP_SET_OPEN_LOOP,
    DcmtStateResult,
    dcmt_parse_state_payload,
    dcmt_send_clear_watchdog_trip,
    dcmt_send_set_brake,
    dcmt_send_set_mode,
    dcmt_send_set_open_loop,
    dcmt_send_set_watchdog,
)
from crumbs_i2c import QUERY_DELAY_S, CrumbsError

from openreactor.config import DeviceConfig
from openreactor.controller import Event
from openreactor.ezo import Outcome, Result, Value
from openreactor.rlht import (
    READS_PER_POLL,
    UNREACHABLE_AFTER,
    WATCHDOG_EVERY,
    SendFailed,
    SliceBusy,
    SliceError,
    SlicePort,
    WatchdogStatus,
    _crumbs_version,
    _describe,
    _watchdog,
)

#: The slice's open-loop PWM runs from -PWM_MAX to PWM_MAX.
PWM_MAX = 255


def duty_to_pwm(duty_pct: float) -> int:
    """Return the PWM for a duty in percent, -100 to 100."""
    return round(duty_pct * PWM_MAX / 100)


def pwm_to_duty(pwm: int) -> float:
    return round(pwm * 100 / PWM_MAX, 1)


@dataclass(frozen=True)
class MotorStatus:
    """One motor as the slice last reported it."""

    motor: int
    channel: str | None  # the config's channel on this motor, if any
    duty_pct: float
    brake: bool


@dataclass(frozen=True)
class DcmtStatus:
    """One DCMT slice as the running controller last saw it."""

    name: str
    address: int
    state: str  # "ok", or why it is out of use
    version: str | None = None  # "DCMT 1.0.0, CRUMBS 0.12.5"
    caps: int | None = None
    mode: str | None = None  # "open loop", "closed-loop position" or "closed-loop speed"
    estop: bool | None = None
    motors: tuple[MotorStatus, ...] = ()
    watchdog: WatchdogStatus | None = None
    # The duties the controller wants, in percent; None for a slice it does
    # not drive.
    desired_pct: tuple[float, float] | None = None


def _mode(state: DcmtStateResult) -> str:
    if state.mode == DCMT_MODE_OPEN_LOOP:
        return "open loop"
    if state.mode == DCMT_MODE_CLOSED_POSITION:
        return "closed-loop position"
    if state.mode == DCMT_MODE_CLOSED_SPEED:
        return "closed-loop speed"
    return f"mode {state.mode}"


class DcmtSlice:
    """One DCMT slice, which stop-all commands and the poll reads."""

    def __init__(
        self,
        device: DeviceConfig,
        port: SlicePort,
        *,
        watchdog_timeout_ms: int,
        poll_s: float,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ):
        self.device = device
        self.name = device.name
        self.port = port
        self.watchdog_timeout_ms = watchdog_timeout_ms
        self.poll_s = poll_s
        self._clock = clock
        self._wall = wall
        self.caps: BreadCapsResult | None = None
        self.identified = False
        self.started = False
        self.protected = False
        # Set when the slice has no command watchdog and its config does not
        # allow that. A read-only slice is sent the safe state and nothing else.
        self.read_only = False
        # Whether a watchdog trip latches on this slice until it is cleared.
        self.latches = False
        # Set while a latched trip holds the motors braked; only the operator
        # clears it (clear_trip), or someone at the slice.
        self.held = False
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
        self._drift_against: list[int] | None = None
        self._drop_result: str | None = None
        self._trip_count = 0
        self._last_state: DcmtStateResult | None = None
        self._last_watchdog: BreadWatchdogResult | None = None
        self._version: str | None = None
        self.unreachable = False
        self.estop = False
        # The PWM openreactor wants on each motor. The operator sets it; any
        # e-stop, trip, reboot or stop-all sets it to 0.
        self.pwm = [0, 0]

    def _event(
        self, kind: str, details: str = "", result: str = "ok", source: str = "system"
    ) -> Event:
        """Return a run-log event about this slice, stamped with the wall clock."""
        return Event(self._wall(), source, kind, device=self.name, details=details, result=result)

    # Start-up blocks, and runs before the controller starts ticking.

    def start(self) -> list[Event]:
        """Check, make safe and arm the slice.

        GET_VERSION must come from a DCMT built with CRUMBS 0.12.0 or later
        and a compatible DCMT module version, and GET_CAPS must offer open
        loop and brakes. Then the safe state. A slice with the watchdog
        capability has it armed with the controller's timeout and confirmed
        with GET_WATCHDOG; one without it is read-only unless its config
        sets ``allow_unprotected``. A latched trip found here is left held,
        and reported.

        Raise SliceError if the slice cannot be used.
        """
        try:
            version = bread_parse_version(self.port.query(BREAD_OP_GET_VERSION))
        except CrumbsError as e:
            raise SliceError(f"no DCMT version reply: {e}") from e
        # It answered as a DCMT, so stop-all includes it from here on, even
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
                version.mod_major, version.mod_minor, DCMT_MODULE_VER_MAJOR, DCMT_MODULE_VER_MINOR
            )
            != 0
        ):
            raise SliceError(
                f"runs DCMT {module}; openreactor needs "
                f"{DCMT_MODULE_VER_MAJOR}.{DCMT_MODULE_VER_MINOR} or a later minor"
            )
        self._version = f"DCMT {module}, CRUMBS {_crumbs_version(version.crumbs_ver)}"
        caps = bread_caps_parse_payload(self.port.query(BREAD_OP_GET_CAPS))
        self.caps = caps
        needed = DCMT_CAP_OPEN_LOOP_CONTROL | DCMT_CAP_BRAKE_CONTROL
        if caps.flags & needed != needed:
            raise SliceError(f"caps 0x{caps.flags:08X} lack open-loop control or brakes")

        self.send_safe()

        found = ""
        if caps.flags & DCMT_CAP_CMD_WATCHDOG:
            self.latches = bool(caps.flags & DCMT_CAP_CLEAR_WATCHDOG_TRIP)
            self.port.send(BREAD_OP_SET_WATCHDOG, dcmt_send_set_watchdog(self.watchdog_timeout_ms))
            watchdog = bread_watchdog_parse_payload(self.port.query(BREAD_OP_GET_WATCHDOG))
            self._last_watchdog = watchdog
            self._trip_count = watchdog.trip_count
            if not watchdog.armed or watchdog.timeout_ms != self.watchdog_timeout_ms:
                raise SliceError(
                    f"watchdog did not arm: armed={watchdog.armed}, "
                    f"timeout {watchdog.timeout_ms} ms, asked for {self.watchdog_timeout_ms} ms"
                )
            self.protected = True
            if watchdog.tripped and self.latches:
                # A trip from before this start, when openreactor last
                # stopped without disarming it or lost the bus. Clearing it
                # is the operator's call.
                self.held = True
                found = "; a watchdog trip is held: clear it to drive the motors"
        else:
            self.read_only = not self.device.allow_unprotected

        self._last_state = dcmt_parse_state_payload(self.port.query(DCMT_OP_GET_STATE))
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
                "slice-start",
                details=f"{self._version}, caps 0x{caps.flags:08X}; {protection}{found}",
                result="held" if self.held else "ok",
            )
        ]

    # Stop-all

    def send_safe(self) -> None:
        """Send the safe state: both brakes on, open loop, then PWM 0.

        Each step is sent even if one before it fails. The brakes go first,
        because a slice in closed-loop position keeps driving to its target
        until it is braked. The desired PWM goes to 0 too.
        """
        self.pwm = [0, 0]
        try:
            self.port.send(DCMT_OP_SET_BRAKE, dcmt_send_set_brake(1, 1))
        finally:
            try:
                self.port.send(DCMT_OP_SET_MODE, dcmt_send_set_mode(DCMT_MODE_OPEN_LOOP))
            finally:
                self.port.send(DCMT_OP_SET_OPEN_LOOP, dcmt_send_set_open_loop(0, 0))

    def forget(self, motor: int) -> None:
        """Set one motor's desired PWM to 0 without sending it.

        For when sending 0 failed or was refused: the next poll then finds
        the slice running another PWM than wanted, and stops it.
        """
        self.pwm[motor - 1] = 0

    def release(self) -> None:
        """Send the safe state, then disarm the watchdog, at a clean shutdown.

        Left armed, the watchdog would trip once openreactor stops polling,
        and a trip that latches would hold the slice until the next start's
        operator cleared it. Disarming only after the safe state was sent
        keeps the watchdog's protection whenever that fails.
        """
        self.send_safe()
        if self.protected and not self.held:
            self.port.send(BREAD_OP_SET_WATCHDOG, dcmt_send_set_watchdog(0))

    def status(self, why: str | None = None) -> DcmtStatus:
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
        elif self.held:
            state = "watchdog trip held: clear it to drive the motors"
        elif self.read_only:
            state = "read-only: the slice has no command watchdog"
        else:
            state = "ok"
        last = self._last_state
        names = {ch.output: ch.name for ch in self.device.channels}
        motors = (
            tuple(
                MotorStatus(m, names.get(m), pwm_to_duty(pwm), bool(last.brakes & (1 << (m - 1))))
                for m, pwm in ((1, last.m1_pwm), (2, last.m2_pwm))
            )
            if last
            else ()
        )
        return DcmtStatus(
            self.name,
            self.device.address,
            state,
            version=self._version,
            caps=self.caps.flags if self.caps else None,
            mode=_mode(last) if last else None,
            estop=bool(last.estop) if last else None,
            motors=motors,
            watchdog=_watchdog(self._last_watchdog) if self._last_watchdog else None,
            desired_pct=(
                (pwm_to_duty(self.pwm[0]), pwm_to_duty(self.pwm[1]))
                if self.started and not self.read_only
                else None
            ),
        )

    def set_duty(self, motor: int, pwm: int) -> None:
        """Send an operator's PWM for one motor, -255 to 255; 0 brakes it.

        Call it on the controller's thread. SET_OPEN_LOOP carries both
        motors' PWM, so the other motor's is sent again too. First, the
        slice is read to confirm it still runs what openreactor last sent,
        with its watchdog armed: after a reboot or an e-stop that no poll
        has seen yet, sending the other motor's old PWM would restart it.
        Then the brakes go, released on each motor that is to run, then the
        PWM: a braked motor's PWM is held at 0 by the slice. The desired PWM
        changes only once both sends succeed.
        """
        if motor not in (1, 2) or not -PWM_MAX <= pwm <= PWM_MAX:
            raise ValueError(f"motor {motor}, PWM {pwm}: the motor is 1 or 2, the PWM -255 to 255")
        if not self.started:
            raise SliceError(f"{self.name} did not finish start-up")
        if self.read_only:
            raise SliceBusy(f"{self.name} is read-only: the slice has no command watchdog")
        if self.estop:
            raise SliceBusy(
                f"the e-stop on {self.name} is held; release it, then set the duty again"
            )
        if self.held:
            raise SliceBusy(f"a watchdog trip is held on {self.name}; clear it first")
        if self.unreachable:
            raise SliceError(f"{self.name} is unreachable: {UNREACHABLE_AFTER} polls failed")
        self._confirm_unchanged()
        wanted = list(self.pwm)
        wanted[motor - 1] = pwm
        try:
            self.port.send(
                DCMT_OP_SET_BRAKE, dcmt_send_set_brake(int(wanted[0] == 0), int(wanted[1] == 0))
            )
            self.port.send(DCMT_OP_SET_OPEN_LOOP, dcmt_send_set_open_loop(wanted[0], wanted[1]))
        except (CrumbsError, OSError) as e:
            raise SendFailed(f"{self.name}: {_describe(e)}") from e
        self.pwm = wanted

    def _confirm_unchanged(self) -> None:
        """Read the slice now, and raise unless it is as openreactor left it.

        Its watchdog must be armed with the controller's timeout, not
        tripped and with the trip count last seen, and its state must show
        no e-stop and the PWM, brakes and mode openreactor last sent.
        Otherwise a check is owed, which the next poll runs, and the caller
        waits for it.
        """
        # Each query stages its own reply over the one the poll staged. The
        # slice builds a reply when it is read, so ending with the query the
        # poll staged leaves it reading a fresh reply of the kind it expects,
        # however often duties are sent.
        watchdog_last = self._staged and self._phase == "watchdog"
        watchdog: BreadWatchdogResult | None = None
        try:
            if self.protected and not watchdog_last:
                watchdog = bread_watchdog_parse_payload(self.port.query(BREAD_OP_GET_WATCHDOG))
            state = dcmt_parse_state_payload(self.port.query(DCMT_OP_GET_STATE))
            if watchdog_last:
                watchdog = bread_watchdog_parse_payload(self.port.query(BREAD_OP_GET_WATCHDOG))
        except (CrumbsError, OSError, ValueError) as e:
            self._staged = False  # what the slice has staged is unknown: stage again
            raise SliceError(f"{self.name}: could not read the slice: {_describe(e)}") from e
        changed = bool(state.estop) or self._drift(state)
        if watchdog is not None:
            # A disarmed watchdog reports a timeout of 0, so the timeout
            # covers a reboot too.
            changed = changed or (
                watchdog.timeout_ms != self.watchdog_timeout_ms
                or watchdog.tripped
                or watchdog.trip_count != self._trip_count
            )
        if changed:
            self._check_owed = True
            raise SliceBusy(
                f"{self.name} changed since it was last checked, and is being checked; "
                "set the duty again in a moment"
            )

    def clear_trip(self) -> list[Event]:
        """Clear a latched watchdog trip at the operator's request.

        Call it on the controller's thread. The motors stay braked at PWM 0:
        the safe state and the watchdog go again after the clear, and the
        next check confirms the trip is gone.
        """
        if not self.started:
            raise SliceError(f"{self.name} did not finish start-up")
        if not self.latches:
            raise SliceBusy(f"{self.name} does not latch watchdog trips; there is nothing to clear")
        if not self.held:
            raise SliceBusy(f"no watchdog trip is held on {self.name}")
        if self.unreachable:
            raise SliceError(f"{self.name} is unreachable: {UNREACHABLE_AFTER} polls failed")
        try:
            self.port.send(BREAD_OP_CLEAR_WATCHDOG_TRIP, dcmt_send_clear_watchdog_trip())
            self.send_safe()
            self.port.send(BREAD_OP_SET_WATCHDOG, dcmt_send_set_watchdog(self.watchdog_timeout_ms))
        except (CrumbsError, OSError) as e:
            raise SendFailed(f"{self.name}: {_describe(e)}") from e
        self.held = False
        self._confirming = True
        self._check_owed = True
        return [
            self._event(
                "slice-trip-cleared",
                details="motors stay stopped until set again",
                source="user",
            )
        ]

    # The poll is RlhtSlice's: GET_STATE every poll_s, staged on one tick and
    # read on a later one, with GET_WATCHDOG on the next tick when a check is
    # owed or WATCHDOG_EVERY good state polls have passed. Every reply feeds
    # the slice's watchdog.

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
        opcode = BREAD_OP_GET_WATCHDOG if self._phase == "watchdog" else DCMT_OP_GET_STATE
        try:
            payload = self.port.read(opcode)
            parsed: BreadWatchdogResult | DcmtStateResult = (
                bread_watchdog_parse_payload(payload)
                if self._phase == "watchdog"
                else dcmt_parse_state_payload(payload)
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
        opcode = BREAD_OP_GET_WATCHDOG if self._phase == "watchdog" else DCMT_OP_GET_STATE
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
            self._failed_checks += 1
            if self._failed_checks < UNREACHABLE_AFTER:
                self._check_owed = True
                return []
            if self._failed_checks > UNREACHABLE_AFTER:
                return []
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
        # It may have restarted or tripped while it did not answer: check it
        # now, as a fresh check, whatever was pending before.
        self._check_owed = self.protected
        self._confirming = False
        return [self._event("slice-reachable")]

    def _estop(self, state: DcmtStateResult) -> list[Result | Event]:
        held = bool(state.estop)
        if held and not self.estop:
            self.estop = True
            # The slice zeroes its PWM while the e-stop is held, and nothing
            # resumes after it until the operator sets a duty again.
            self.pwm = [0, 0]
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
                    details="e-stop released; motors stay stopped until set again",
                    result="released",
                    source="slice",
                )
            ]
        return []

    def _drift(self, state: DcmtStateResult) -> bool:
        """Return whether the slice runs another PWM, brakes or mode than openreactor wants.

        A held e-stop or trip is handled on its own, and a read-only slice
        is not openreactor's to set, so none of them counts.
        """
        if self.estop or self.held or self.read_only:
            return False
        brakes = int(self.pwm[0] == 0) | int(self.pwm[1] == 0) << 1
        return (
            state.mode != DCMT_MODE_OPEN_LOOP
            or [state.m1_pwm, state.m2_pwm] != self.pwm
            or state.brakes != brakes
        )

    def _drifted(self, state: DcmtStateResult) -> list[Result | Event]:
        """Owe a watchdog check when the PWM drifted, or stop the motors if no check can run.

        A trip or a reboot zeroes the slice's PWM, and the check finds
        either; if it finds neither, ``_supervise`` stops the motors.
        """
        if not self._drift(state):
            self._drift_against = None
            self._drop_result = None
            return []
        if self.protected and self._failed_checks < UNREACHABLE_AFTER:
            self._drift_against = list(self.pwm)
            self._check_owed = True
            return []
        self._drift_against = None
        return self._drop(state)

    def _drop(self, state: DcmtStateResult) -> list[Result | Event]:
        """Send the safe state after the PWM or mode changed with no trip or reboot.

        The cause may be an e-stop pressed and released between two polls, a
        duty that reached the slice although sending it failed, or another
        controller. Nothing resumes until the operator sets a duty again.
        """
        ran = f"{_mode(state)} at {pwm_to_duty(state.m1_pwm):g} and {pwm_to_duty(state.m2_pwm):g} %"
        wanted = f"open loop at {pwm_to_duty(self.pwm[0]):g} and {pwm_to_duty(self.pwm[1]):g} %"
        try:
            self.send_safe()  # zeroes the desired PWM too
            result = "safe state sent"
        except (CrumbsError, OSError) as e:
            self.pwm = [0, 0]
            result = f"error: safe state not sent: {_describe(e)}"
        if result == self._drop_result:
            return []  # retried each poll, reported once per result
        self._drop_result = result
        return [
            self._event(
                "slice-duty-changed",
                details=(
                    f"the slice ran {ran}, not {wanted}, with no trip or reboot to explain "
                    "it; motors stay stopped until set again"
                ),
                result=result,
            )
        ]

    def _supervise(self, watchdog: BreadWatchdogResult) -> list[Result | Event]:
        """Act on a watchdog check.

        A reboot, or a trip on a slice that does not latch, means the slice
        dropped its state: send the safe state and arm it again, and check
        in the next cycle that it took. A trip on a slice that latches is
        held, and reported once. With neither, stop motors whose PWM the
        last GET_STATE found had drifted.
        """
        self._failed_checks = 0
        self._last_watchdog = watchdog
        against, self._drift_against = self._drift_against, None
        baseline = self._trip_count
        self._trip_count = watchdog.trip_count
        confirming, self._confirming = self._confirming, False
        if not watchdog.armed:
            # A reboot, or the watchdog disarmed at the slice. A latched trip
            # still held is found again by the check after the re-assert.
            what = "slice-reboot"
            details = f"watchdog disarmed (trips {watchdog.trip_count})"
            self.held = False
        elif watchdog.timeout_ms != self.watchdog_timeout_ms:
            what = "slice-watchdog-changed"
            details = (
                f"watchdog timeout {watchdog.timeout_ms} ms, not {self.watchdog_timeout_ms} ms"
            )
            self.held = False
        elif self.held:
            if watchdog.tripped:
                return []  # still held; reported when it began
            # Cleared at the slice itself. It stays braked; make it safe and
            # arm it again like any slice that dropped its state.
            what = "slice-trip"
            details = f"watchdog trip cleared on the slice (trips {watchdog.trip_count})"
            self.held = False
        elif watchdog.tripped or watchdog.trip_count != baseline:
            what = "slice-trip"
            details = (
                f"watchdog tripped (tripped={watchdog.tripped}, "
                f"trips {baseline} -> {watchdog.trip_count})"
            )
            if watchdog.tripped and self.latches:
                self.held = True
                self.pwm = [0, 0]
                return [
                    self._event(
                        what,
                        details=details + "; motors braked until the trip is cleared",
                        result="held",
                    )
                ]
        else:
            last = self._last_state
            if last is None or against is None or against != self.pwm:
                return []
            return self._drop(last)
        details += "; motors stay stopped until set again"
        if confirming:
            return [self._event(what, details=details, result="error: the re-assert did not take")]
        try:
            self._reassert()
        except (CrumbsError, OSError) as e:
            self._trip_count = baseline
            self._check_owed = True
            failure = f"error: re-assert failed: {_describe(e)}"
            return [self._event(what, details=details, result=failure)]
        self._confirming = True
        self._check_owed = True
        return [self._event(what, details=details, result="re-asserted")]

    def _reassert(self) -> None:
        """Send the safe state, then arm the watchdog again."""
        self.send_safe()
        if self.protected:
            self.port.send(BREAD_OP_SET_WATCHDOG, dcmt_send_set_watchdog(self.watchdog_timeout_ms))

    def _send_safe_quietly(self) -> None:
        # A failure is retried at the next poll; the e-stop holds the motors.
        with suppress(CrumbsError, OSError):
            self.send_safe()

    def _failed(self, e: BaseException) -> list[Result]:
        detail = _describe(e)
        return [Result(ch.name, Outcome.ERROR, detail=detail) for ch in self.device.channels]

    def _readings(self, state: DcmtStateResult) -> list[Result]:
        results: list[Result] = []
        for ch in self.device.channels:
            pwm = state.m1_pwm if ch.output == 1 else state.m2_pwm
            results.append(Result(ch.name, Outcome.OK, (Value("duty", pwm_to_duty(pwm), "%"),)))
        return results
