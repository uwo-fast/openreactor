import struct

import pytest
from fakes import (
    CLEAR_WATCHDOG_TRIP,
    DCMT_SET_BRAKE,
    DCMT_SET_MODE,
    DCMT_SET_OPEN_LOOP,
    SET_WATCHDOG,
    FakeClock,
    FakeDcmt,
    FakeI2cBus,
)

from openreactor.config import ChannelConfig, DeviceConfig
from openreactor.controller import Controller
from openreactor.dcmt import DcmtSlice, duty_to_pwm, pwm_to_duty
from openreactor.ezo import EzoReader, Outcome, Result
from openreactor.rlht import CrumbsPort, SendFailed, SliceBusy, SliceError

STIRRER = ChannelConfig(name="stirrer", label="Stirrer", output=1)
PUMP = ChannelConfig(name="pump", label="Pump", output=2)


def device(*channels: ChannelConfig, allow_unprotected: bool = False) -> DeviceConfig:
    return DeviceConfig(
        name="motors",
        kind="dcmt",
        bus="/dev/i2c-1",
        address=0x14,
        channels=channels or (STIRRER, PUMP),
        allow_unprotected=allow_unprotected,
    )


def made(dcmt: FakeDcmt, config: DeviceConfig | None = None) -> tuple[DcmtSlice, FakeClock]:
    clock = FakeClock()
    s = DcmtSlice(
        config or device(),
        CrumbsPort(FakeI2cBus({0x14: dcmt}), 0x14, type_id=0x02, sleep=clock.sleep),
        watchdog_timeout_ms=5000,
        poll_s=1.0,
        clock=clock,
    )
    return s, clock


def started(dcmt: FakeDcmt, config: DeviceConfig | None = None) -> tuple[DcmtSlice, FakeClock]:
    s, clock = made(dcmt, config)
    s.start()
    return s, clock


def ops(dcmt: FakeDcmt) -> list[int]:
    return [op for op, _ in dcmt.commands]


def controller_for(s: DcmtSlice, clock: FakeClock) -> tuple[Controller, list]:
    seen: list = []
    c = Controller(EzoReader([]), [s], polled=[s], ezo_period_s=2.0, clock=clock, sleep=clock.sleep)
    c.subscribe(seen.append)
    return c, seen


def tick(c: Controller, clock: FakeClock, seconds: float) -> None:
    for _ in range(round(seconds / 0.1)):
        c.step()
        clock.now = round(clock.now + 0.1, 9)


def events(seen: list, kind: str) -> list:
    return [e for e in seen if getattr(e, "kind", None) == kind]


SAFE = [DCMT_SET_BRAKE, DCMT_SET_MODE, DCMT_SET_OPEN_LOOP]


# Start-up


def test_start_up_brakes_first_then_open_loop_then_zero_and_arms():
    dcmt = FakeDcmt()
    dcmt.mode = 1  # left in closed-loop position by someone else
    s, _ = started(dcmt)
    assert ops(dcmt) == [*SAFE, SET_WATCHDOG]
    assert dcmt.commands[0] == (DCMT_SET_BRAKE, bytes((1, 1)))
    assert dcmt.commands[1] == (DCMT_SET_MODE, bytes((0,)))
    assert dcmt.commands[2] == (DCMT_SET_OPEN_LOOP, struct.pack("<hh", 0, 0))
    assert dcmt.commands[3] == (SET_WATCHDOG, struct.pack("<H", 5000))
    assert (dcmt.mode, dcmt.brakes, dcmt.armed) == (0, [1, 1], 1)
    assert s.protected and s.latches and not s.held


def test_start_up_reports_what_it_found():
    s, _ = made(FakeDcmt())
    [event] = s.start()
    assert (event.kind, event.device, event.result) == ("slice-start", "motors", "ok")
    assert event.details == "DCMT 1.0.0, CRUMBS 0.12.5, caps 0x00000077; watchdog armed at 5000 ms"


@pytest.mark.parametrize(
    ("fake", "message"),
    [
        (FakeDcmt(type_id=0x01), "no DCMT version reply: expected type 0x02"),
        (FakeDcmt(crumbs_version=1100), "built with CRUMBS 0.11.0; 0.12.0 or later is needed"),
        (FakeDcmt(module=(2, 0, 0)), "runs DCMT 2.0.0; openreactor needs 1.0 or a later minor"),
        (FakeDcmt(caps=0x75), "lack open-loop control or brakes"),  # no brakes
        (FakeDcmt(caps=0x76), "lack open-loop control or brakes"),  # no open loop
    ],
)
def test_a_slice_that_is_not_a_usable_dcmt_is_refused_before_any_command(fake, message):
    with pytest.raises(SliceError, match=message):
        started(fake)
    assert fake.commands == []


def test_a_slice_without_a_watchdog_is_read_only_unless_allowed():
    dcmt = FakeDcmt(caps=0x17)  # no watchdog
    s, _ = started(dcmt)
    assert ops(dcmt) == SAFE
    assert s.read_only and not s.protected
    with pytest.raises(SliceBusy, match="read-only"):
        s.set_duty(1, 100)

    s, _ = started(FakeDcmt(caps=0x17), device(allow_unprotected=True))
    assert not s.read_only
    s.set_duty(1, 100)


def test_a_trip_held_from_before_start_up_is_left_held_and_reported():
    dcmt = FakeDcmt()
    dcmt.armed, dcmt.timeout_ms = 1, 5000
    dcmt.trip()
    s, _ = made(dcmt)
    [event] = s.start()
    assert event.result == "held"
    assert "a watchdog trip is held: clear it to drive the motors" in event.details
    assert s.held and dcmt.tripped == 1
    assert CLEAR_WATCHDOG_TRIP not in ops(dcmt)
    with pytest.raises(SliceBusy, match="clear it first"):
        s.set_duty(1, 100)


# Setting a duty


def test_a_duty_releases_that_motors_brake_before_its_pwm():
    dcmt = FakeDcmt()
    s, _ = started(dcmt)
    dcmt.commands.clear()
    s.set_duty(1, 128)
    assert dcmt.commands == [
        (DCMT_SET_BRAKE, bytes((0, 1))),
        (DCMT_SET_OPEN_LOOP, struct.pack("<hh", 128, 0)),
    ]
    assert (dcmt.pwm, dcmt.brakes) == ([128, 0], [0, 1])
    s.set_duty(2, -255)
    assert (dcmt.pwm, dcmt.brakes) == ([128, -255], [0, 0])
    s.set_duty(1, 0)
    assert (dcmt.pwm, dcmt.brakes) == ([0, -255], [1, 0])
    assert s.pwm == [0, -255]


@pytest.mark.parametrize(("motor", "pwm"), [(0, 100), (3, 100), (1, 256), (2, -256)])
def test_a_duty_outside_the_slices_range_is_refused_before_any_send(motor, pwm):
    dcmt = FakeDcmt()
    s, _ = started(dcmt)
    dcmt.commands.clear()
    with pytest.raises(ValueError):
        s.set_duty(motor, pwm)
    assert dcmt.commands == []


def test_a_duty_is_refused_after_a_reboot_no_poll_has_seen():
    dcmt = FakeDcmt()
    s, clock = started(dcmt)
    s.set_duty(2, 100)
    c, seen = controller_for(s, clock)
    tick(c, clock, 1.0)
    dcmt.reboot()
    with pytest.raises(SliceBusy, match="changed since it was last checked"):
        s.set_duty(1, 50)
    # Motor 2's old PWM was not sent again to a slice with no watchdog.
    assert (dcmt.pwm, dcmt.armed) == ([0, 0], 0)
    tick(c, clock, 1.0)  # the check owed runs in the next poll's cycle
    [reboot] = events(seen, "slice-reboot")
    assert reboot.result == "re-asserted" and dcmt.armed == 1 and s.pwm == [0, 0]


def test_a_duty_is_refused_after_an_e_stop_pressed_and_released_between_polls():
    dcmt = FakeDcmt()
    s, clock = started(dcmt)
    s.set_duty(2, 100)
    dcmt.press_estop()
    dcmt.release_estop()
    with pytest.raises(SliceBusy, match="changed since it was last checked"):
        s.set_duty(1, 50)
    assert dcmt.pwm == [0, 0]


def test_a_duty_is_refused_while_the_e_stop_is_held_but_unseen():
    dcmt = FakeDcmt()
    s, _ = started(dcmt)
    dcmt.press_estop()
    with pytest.raises(SliceBusy, match="changed since it was last checked"):
        s.set_duty(1, 50)
    dcmt.release_estop()
    assert dcmt.pwm == [0, 0]


def test_a_duty_is_refused_when_the_watchdog_timeout_changed_at_the_slice():
    dcmt = FakeDcmt()
    s, clock = started(dcmt)
    c, seen = controller_for(s, clock)
    dcmt.timeout_ms = 1000  # WDOG=1000 on the slice's serial port
    with pytest.raises(SliceBusy):
        s.set_duty(1, 50)
    tick(c, clock, 1.0)
    [changed] = events(seen, "slice-watchdog-changed")
    assert changed.result == "re-asserted" and dcmt.timeout_ms == 5000


def test_a_duty_is_refused_when_the_watchdog_was_disarmed_at_the_slice():
    dcmt = FakeDcmt()
    s, clock = started(dcmt)
    c, seen = controller_for(s, clock)
    dcmt.armed, dcmt.timeout_ms = 0, 0  # WDOG=0 on the slice's serial port
    with pytest.raises(SliceBusy, match="changed since it was last checked"):
        s.set_duty(1, 50)
    assert dcmt.pwm == [0, 0]
    tick(c, clock, 1.0)
    assert events(seen, "slice-reboot")[0].result == "re-asserted" and dcmt.armed == 1


def test_a_duty_is_refused_while_another_controller_drives_the_slice():
    dcmt = FakeDcmt()
    s, clock = started(dcmt)
    c, _ = controller_for(s, clock)
    dcmt.pwm = [50, 0]
    dcmt.brakes = [0, 1]
    with pytest.raises(SliceBusy, match="changed since it was last checked"):
        s.set_duty(2, 100)
    assert dcmt.pwm == [50, 0]  # nothing sent; the next poll stops it
    tick(c, clock, 3.0)
    assert dcmt.pwm == [0, 0]


def test_a_failed_send_keeps_the_desired_duty():
    dcmt = FakeDcmt()
    s, _ = started(dcmt)
    dcmt.fail_opcodes[DCMT_SET_OPEN_LOOP] = OSError(5, "I/O error")
    with pytest.raises(SendFailed):
        s.set_duty(1, 100)
    assert s.pwm == [0, 0]


def test_duty_and_pwm_convert_both_ways():
    assert [duty_to_pwm(d) for d in (-100, -50, 0, 50, 100)] == [-255, -128, 0, 128, 255]
    assert [pwm_to_duty(p) for p in (-255, 128, 0, 255)] == [-100.0, 50.2, 0.0, 100.0]


# Polling and stop-all


def test_the_poll_publishes_each_channels_duty():
    dcmt = FakeDcmt()
    s, clock = started(dcmt)
    s.set_duty(1, 255)
    c, seen = controller_for(s, clock)
    tick(c, clock, 1.0)
    results = {r.channel: r for r in seen if isinstance(r, Result)}
    assert [(v.field, v.value, v.unit) for v in results["stirrer"].values] == [("duty", 100.0, "%")]
    assert results["pump"].values[0].value == 0.0


def test_stop_all_brakes_both_motors_and_clears_the_desired_duty():
    dcmt = FakeDcmt()
    s, clock = started(dcmt)
    s.set_duty(1, 200)
    c, _ = controller_for(s, clock)
    dcmt.commands.clear()
    c.stop_all("user")
    tick(c, clock, 0.1)
    assert ops(dcmt) == SAFE
    assert (dcmt.pwm, dcmt.brakes, s.pwm) == ([0, 0], [1, 1], [0, 0])


def test_an_e_stop_sends_stop_all_and_nothing_resumes():
    dcmt = FakeDcmt()
    s, clock = started(dcmt)
    s.set_duty(1, 200)
    c, seen = controller_for(s, clock)
    dcmt.press_estop()
    tick(c, clock, 2.0)
    [held] = events(seen, "e-stop")
    assert (held.result, held.source, held.device) == ("held", "slice", "motors")
    assert events(seen, "stop-all")
    assert s.pwm == [0, 0]
    with pytest.raises(SliceBusy, match="e-stop"):
        s.set_duty(1, 200)
    dcmt.release_estop()
    tick(c, clock, 2.0)
    released = events(seen, "e-stop")[-1]
    assert released.result == "released" and "stay stopped" in released.details
    tick(c, clock, 10.0)
    assert dcmt.pwm == [0, 0] and dcmt.brakes == [1, 1]
    assert not events(seen, "slice-duty-changed")


def test_an_e_stop_clears_the_desired_duty_before_any_stop_all():
    dcmt = FakeDcmt()
    s, clock = started(dcmt)
    s.set_duty(1, 200)
    dcmt.press_estop()
    out = []
    for _ in range(5):  # the poll alone, with no controller to send stop-all
        out += s.advance(clock())
        clock.now += 0.1
    assert [e.result for e in out if getattr(e, "kind", None) == "e-stop"] == ["held"]
    assert s.pwm == [0, 0]


# Supervision


def test_a_latched_trip_is_held_and_reported_once():
    dcmt = FakeDcmt()
    s, clock = started(dcmt)
    s.set_duty(1, 200)
    c, seen = controller_for(s, clock)
    dcmt.trip()
    dcmt.commands.clear()
    tick(c, clock, 12.0)
    [trip] = events(seen, "slice-trip")
    assert trip.result == "held" and "until the trip is cleared" in trip.details
    assert s.held and s.pwm == [0, 0]
    # Nothing is sent to clear it or move a motor; the poll only reads.
    assert ops(dcmt) == []
    assert not events(seen, "slice-duty-changed")
    assert s.status().state == "watchdog trip held: clear it to drive the motors"


def test_clearing_a_held_trip_leaves_the_motors_stopped():
    dcmt = FakeDcmt()
    s, clock = started(dcmt)
    s.set_duty(1, 200)
    c, seen = controller_for(s, clock)
    dcmt.trip()
    tick(c, clock, 6.0)
    assert s.held
    dcmt.commands.clear()
    [cleared] = s.clear_trip()
    assert (cleared.kind, cleared.source) == ("slice-trip-cleared", "user")
    assert ops(dcmt) == [CLEAR_WATCHDOG_TRIP, *SAFE, SET_WATCHDOG]
    assert dcmt.commands[0] == (CLEAR_WATCHDOG_TRIP, b"")
    assert (dcmt.tripped, dcmt.pwm, dcmt.brakes, dcmt.armed) == (0, [0, 0], [1, 1], 1)
    tick(c, clock, 3.0)
    # The confirming check found it armed and clear: no new trip event.
    assert len(events(seen, "slice-trip")) == 1
    s.set_duty(2, 100)
    assert dcmt.pwm == [0, 100]


def test_clear_trip_is_refused_when_nothing_is_held_or_the_slice_does_not_latch():
    dcmt = FakeDcmt()
    s, _ = started(dcmt)
    with pytest.raises(SliceBusy, match="no watchdog trip is held"):
        s.clear_trip()
    s, _ = started(FakeDcmt(caps=0x37, latches=False))
    with pytest.raises(SliceBusy, match="does not latch"):
        s.clear_trip()
    assert CLEAR_WATCHDOG_TRIP not in ops(dcmt)


def test_a_trip_cleared_at_the_slice_is_made_safe_and_re_armed():
    dcmt = FakeDcmt()
    s, clock = started(dcmt)
    c, seen = controller_for(s, clock)
    dcmt.trip()
    tick(c, clock, 6.0)
    dcmt.tripped = 0  # WDCLEAR on the slice's serial port
    dcmt.commands.clear()
    tick(c, clock, 6.0)
    trip = events(seen, "slice-trip")[-1]
    assert trip.result == "re-asserted" and "cleared on the slice" in trip.details
    assert ops(dcmt)[:4] == [*SAFE, SET_WATCHDOG]
    assert not s.held


def test_a_trip_on_a_slice_that_does_not_latch_is_re_asserted_and_resumes_nothing():
    dcmt = FakeDcmt(caps=0x37, latches=False)
    s, clock = started(dcmt)
    s.set_duty(1, 200)
    c, seen = controller_for(s, clock)
    dcmt.trip()
    dcmt.commands.clear()
    tick(c, clock, 6.0)
    [trip] = events(seen, "slice-trip")
    assert trip.result == "re-asserted" and "stay stopped until set again" in trip.details
    assert ops(dcmt)[:4] == [*SAFE, SET_WATCHDOG]
    assert (dcmt.pwm, s.pwm, dcmt.tripped) == ([0, 0], [0, 0], 0)


def test_a_reboot_is_made_safe_and_re_armed_and_resumes_nothing():
    dcmt = FakeDcmt()
    s, clock = started(dcmt)
    s.set_duty(1, 200)
    c, seen = controller_for(s, clock)
    dcmt.reboot()
    dcmt.commands.clear()
    tick(c, clock, 6.0)
    [reboot] = events(seen, "slice-reboot")
    assert reboot.result == "re-asserted"
    assert ops(dcmt)[:4] == [*SAFE, SET_WATCHDOG]
    assert (dcmt.armed, dcmt.brakes, dcmt.pwm, s.pwm) == (1, [1, 1], [0, 0], [0, 0])


def test_a_duty_changed_with_no_trip_or_reboot_stops_the_motors():
    dcmt = FakeDcmt()
    s, clock = started(dcmt)
    s.set_duty(1, 200)
    c, seen = controller_for(s, clock)
    dcmt.pwm = [50, 0]  # another controller on the bus
    tick(c, clock, 3.0)
    [changed] = events(seen, "slice-duty-changed")
    assert changed.result == "safe state sent"
    assert (dcmt.pwm, dcmt.brakes, s.pwm) == ([0, 0], [1, 1], [0, 0])


def test_a_brake_left_released_by_a_failed_send_stops_the_motors():
    dcmt = FakeDcmt()
    s, clock = started(dcmt)
    c, seen = controller_for(s, clock)
    dcmt.fail_opcodes[DCMT_SET_OPEN_LOOP] = OSError(5, "I/O error")
    with pytest.raises(SendFailed):
        s.set_duty(1, 100)
    assert dcmt.brakes == [0, 1]  # the brake went, the PWM did not
    del dcmt.fail_opcodes[DCMT_SET_OPEN_LOOP]
    tick(c, clock, 3.0)
    [changed] = events(seen, "slice-duty-changed")
    assert changed.result == "safe state sent" and dcmt.brakes == [1, 1]


def test_a_mode_changed_by_someone_else_stops_the_motors():
    dcmt = FakeDcmt()
    s, clock = started(dcmt)
    c, seen = controller_for(s, clock)
    dcmt.mode = 1
    tick(c, clock, 3.0)
    [changed] = events(seen, "slice-duty-changed")
    assert "closed-loop position" in changed.details
    assert dcmt.mode == 0


def test_a_re_assert_that_did_not_take_is_reported_and_not_repeated_at_once():
    dcmt = FakeDcmt()
    s, clock = started(dcmt)
    c, seen = controller_for(s, clock)
    dcmt.reboot()
    until_event(c, clock, seen, "slice-reboot")
    dcmt.reboot()  # again, before the confirming check
    dcmt.commands.clear()
    tick(c, clock, 1.0)
    [first, second] = events(seen, "slice-reboot")
    assert first.result == "re-asserted"
    assert second.result == "error: the re-assert did not take"
    assert SET_WATCHDOG not in ops(dcmt)


def until_event(c: Controller, clock: FakeClock, seen: list, kind: str) -> None:
    for _ in range(200):
        if events(seen, kind):
            return
        tick(c, clock, 0.1)
    raise AssertionError(f"no {kind} event")


def test_a_slice_that_answers_again_is_checked_at_once():
    dcmt = FakeDcmt()
    s, clock = started(dcmt)
    c, seen = controller_for(s, clock)
    dcmt.faults = [OSError(121, "Remote I/O error")] * 1000
    tick(c, clock, 4.0)
    assert s.unreachable
    dcmt.faults = []
    dcmt.reboot()  # it lost power while it did not answer
    tick(c, clock, 1.0)  # one poll, well before the fifth
    assert events(seen, "slice-reachable")
    assert events(seen, "slice-reboot")[0].result == "re-asserted"


def test_a_trip_while_unreachable_is_found_by_the_first_poll_that_answers():
    dcmt = FakeDcmt()
    s, clock = started(dcmt)
    c, seen = controller_for(s, clock)
    dcmt.faults = [OSError(121, "Remote I/O error")] * 1000
    tick(c, clock, 4.0)
    assert s.unreachable
    dcmt.trip()  # the bus was gone longer than the watchdog's timeout
    dcmt.faults = []
    tick(c, clock, 1.0)  # one poll, well before the fifth
    [trip] = events(seen, "slice-trip")
    assert trip.result == "held" and s.held


def test_three_failed_polls_make_the_slice_unreachable():
    dcmt = FakeDcmt()
    s, clock = started(dcmt)
    c, seen = controller_for(s, clock)
    dcmt.faults = [OSError(121, "Remote I/O error")] * 1000
    tick(c, clock, 4.0)
    assert s.unreachable and events(seen, "slice-unreachable")
    errors = [r for r in seen if isinstance(r, Result) and r.outcome is Outcome.ERROR]
    assert {r.channel for r in errors} == {"stirrer", "pump"}
    with pytest.raises(SliceError, match="unreachable"):
        s.set_duty(1, 100)


# Shutdown


def test_release_makes_the_slice_safe_then_disarms_its_watchdog():
    dcmt = FakeDcmt()
    s, _ = started(dcmt)
    s.set_duty(1, 200)
    dcmt.commands.clear()
    s.release()
    assert ops(dcmt) == [*SAFE, SET_WATCHDOG]
    assert dcmt.commands[-1] == (SET_WATCHDOG, struct.pack("<H", 0))
    assert (dcmt.armed, dcmt.pwm) == (0, [0, 0])


def test_release_leaves_a_held_slice_armed():
    dcmt = FakeDcmt()
    dcmt.armed, dcmt.timeout_ms = 1, 5000
    dcmt.trip()
    s, _ = started(dcmt)
    assert s.held
    s.release()
    assert (dcmt.armed, dcmt.tripped) == (1, 1)


def test_release_keeps_the_watchdog_armed_if_the_safe_state_fails():
    dcmt = FakeDcmt()
    s, _ = started(dcmt)
    dcmt.fail_opcodes[DCMT_SET_OPEN_LOOP] = OSError(5, "I/O error")
    with pytest.raises(OSError):
        s.release()
    assert dcmt.armed == 1
