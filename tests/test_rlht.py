import errno
import struct

import pytest
from fakes import FakeActuator, FakeClock, FakeI2cBus, FakeRlht

from openreactor.config import ChannelConfig, DeviceConfig
from openreactor.controller import Controller
from openreactor.ezo import EzoReader, Outcome, Result
from openreactor.rlht import (
    READS_PER_POLL,
    CrumbsPort,
    RlhtSlice,
    SendFailed,
    SliceBusy,
    SliceError,
    opened_slices,
)

SET_SETPOINTS, SET_OPEN_DUTY, SET_MODE, SET_PID, SET_PERIODS, SET_TC = (
    0x02,
    0x06,
    0x01,
    0x03,
    0x04,
    0x05,
)
SET_WATCHDOG = 0x7E
GET_WATCHDOG = 0x7D
JACKET = ChannelConfig(name="jacket", label="Jacket", output=1, tc=1)


def device(*channels: ChannelConfig, allow_unprotected: bool = False) -> DeviceConfig:
    return DeviceConfig(
        name="heater",
        kind="rlht",
        bus="/dev/i2c-1",
        address=0x0A,
        channels=channels or (JACKET,),
        allow_unprotected=allow_unprotected,
    )


def started(rlht: FakeRlht, config: DeviceConfig | None = None) -> tuple[RlhtSlice, FakeClock]:
    clock = FakeClock()
    s = RlhtSlice(
        config or device(),
        CrumbsPort(FakeI2cBus({0x0A: rlht}), 0x0A, sleep=clock.sleep),
        watchdog_timeout_ms=5000,
        poll_s=1.0,
        clock=clock,
        sleep=clock.sleep,
    )
    s.start()
    return s, clock


def ops(rlht: FakeRlht) -> list[int]:
    return [op for op, _ in rlht.commands]


# Start-up


def test_start_up_sends_the_safe_state_before_arming_then_configures():
    rlht = FakeRlht()
    s, _ = started(rlht)
    assert ops(rlht) == [SET_SETPOINTS, SET_OPEN_DUTY, SET_WATCHDOG, SET_MODE, SET_TC]
    assert rlht.commands[0] == (SET_SETPOINTS, struct.pack("<hh", 0, 0))
    assert rlht.commands[1] == (SET_OPEN_DUTY, bytes((0, 0)))
    assert rlht.commands[2] == (SET_WATCHDOG, struct.pack("<H", 5000))
    assert (rlht.armed, rlht.timeout_ms) == (1, 5000)
    assert s.protected and not s.read_only
    assert rlht.mode == 0  # closed loop


def test_start_up_reports_what_it_found():
    rlht = FakeRlht()
    clock = FakeClock()
    s = RlhtSlice(
        device(),
        CrumbsPort(FakeI2cBus({0x0A: rlht}), 0x0A, sleep=clock.sleep),
        watchdog_timeout_ms=5000,
        poll_s=1.0,
        clock=clock,
        sleep=clock.sleep,
    )
    [event] = s.start()
    assert (event.kind, event.device) == ("slice-start", "heater")
    assert event.details == "RLHT 1.0.0, CRUMBS 0.15.0, caps 0x0000007F; watchdog armed at 5000 ms"


@pytest.mark.parametrize(
    ("fake", "message"),
    [
        (FakeRlht(type_id=0x02), "no RLHT version reply: expected type 0x01"),
        (FakeRlht(crumbs_version=1100), "built with CRUMBS 0.11.0; 0.12.0 or later is needed"),
        (FakeRlht(module=(2, 0, 0)), "runs RLHT 2.0.0; openreactor needs 1.0 or a later minor"),
    ],
)
def test_a_slice_that_is_not_a_usable_rlht_is_refused_before_any_command(fake, message):
    with pytest.raises(SliceError, match=message):
        started(fake)
    assert fake.commands == []


def test_a_later_module_minor_is_accepted():
    s, _ = started(FakeRlht(module=(1, 3, 2)))
    assert s.protected


def test_arming_is_gated_on_the_watchdog_capability():
    rlht = FakeRlht(caps=0x3F)  # the six controls, no watchdog
    s, _ = started(rlht)
    assert SET_WATCHDOG not in ops(rlht)
    assert not s.protected


def test_a_slice_without_a_watchdog_is_read_only_after_the_safe_state():
    rlht = FakeRlht(caps=0x3F)
    s, _ = started(rlht)
    assert s.read_only
    assert ops(rlht) == [SET_SETPOINTS, SET_OPEN_DUTY]


def test_allow_unprotected_lets_a_slice_without_a_watchdog_be_configured():
    rlht = FakeRlht(caps=0x3F)
    s, _ = started(rlht, device(allow_unprotected=True))
    assert not s.read_only and not s.protected
    assert ops(rlht) == [SET_SETPOINTS, SET_OPEN_DUTY, SET_MODE, SET_TC]


def test_a_watchdog_that_does_not_arm_is_refused():
    with pytest.raises(SliceError, match="watchdog did not arm: armed=0"):
        started(FakeRlht(arms=False))


def test_gains_go_only_when_the_config_gives_both_outputs():
    rlht = FakeRlht()
    jacket = ChannelConfig(
        "jacket", "Jacket", output=1, tc=2, kp=2.3, ki=0.1, kd=0.0, period_ms=2000
    )
    lid = ChannelConfig("lid", "Lid", output=2, tc=1, kp=1.0, ki=0.5, kd=0.2)
    started(rlht, device(jacket, lid))
    assert ops(rlht) == [
        SET_SETPOINTS,
        SET_OPEN_DUTY,
        SET_WATCHDOG,
        SET_MODE,
        SET_PERIODS,
        SET_TC,
        SET_PID,
    ]
    commands = dict(rlht.commands)
    assert commands[SET_TC] == bytes((2, 1))
    # The lid has no period: it keeps the slice's own.
    assert commands[SET_PERIODS] == struct.pack("<HH", 2000, 1000)
    assert commands[SET_PID] == bytes((23, 1, 0, 10, 5, 2))


def test_gains_on_one_output_are_never_filled_in():
    """Both outputs' gains go in one command and cannot be read back: with
    only one output's gains configured, none are sent, rather than zeros
    that would freeze the other output's PID integral."""
    rlht = FakeRlht()
    tuned = ChannelConfig(
        "jacket", "Jacket", output=1, tc=2, kp=2.3, ki=0.1, kd=0.0, period_ms=2000
    )
    started(rlht, device(tuned))
    assert SET_PID not in ops(rlht)
    # Output 2 has no channel: it keeps its thermocouple and period.
    commands = dict(rlht.commands)
    assert commands[SET_TC] == bytes((2, 2))
    assert commands[SET_PERIODS] == struct.pack("<HH", 2000, 1000)


def test_a_control_the_slice_lacks_is_not_sent():
    rlht = FakeRlht(caps=0x40 | 0x02 | 0x20)  # watchdog, setpoints, open duty only
    jacket = ChannelConfig(
        "jacket", "Jacket", output=1, tc=1, kp=1.0, ki=1.0, kd=1.0, period_ms=500
    )
    lid = ChannelConfig("lid", "Lid", output=2, tc=2, kp=1.0, ki=1.0, kd=1.0, period_ms=500)
    started(rlht, device(jacket, lid))
    assert ops(rlht) == [SET_SETPOINTS, SET_OPEN_DUTY, SET_WATCHDOG]


def test_a_corrupt_start_up_read_is_retried():
    rlht = FakeRlht()
    rlht.faults = ["corrupt"]
    s, _ = started(rlht)
    assert s.protected


# The safe state


def test_the_safe_state_is_both_stop_ops_in_order():
    rlht = FakeRlht()
    s, _ = started(rlht)
    rlht.commands.clear()
    s.send_safe()
    assert rlht.commands == [
        (SET_SETPOINTS, struct.pack("<hh", 0, 0)),
        (SET_OPEN_DUTY, bytes((0, 0))),
    ]
    assert SET_MODE not in ops(rlht)  # never a switch to open loop as a stop


def test_the_second_stop_op_is_sent_even_when_the_first_fails():
    rlht = FakeRlht()
    s, _ = started(rlht)
    rlht.commands.clear()
    rlht.fail_opcodes[SET_SETPOINTS] = OSError(errno.EREMOTEIO, "Remote I/O error")
    with pytest.raises(OSError):
        s.send_safe()
    assert ops(rlht) == [SET_OPEN_DUTY]


def test_the_second_stop_op_is_sent_even_when_the_first_is_interrupted():
    rlht = FakeRlht()
    s, _ = started(rlht)
    rlht.commands.clear()
    rlht.fail_opcodes[SET_SETPOINTS] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        s.send_safe()
    assert ops(rlht) == [SET_OPEN_DUTY]


def test_stop_all_reaches_the_slice_and_every_other_actuator():
    rlht = FakeRlht()
    s, clock = started(rlht)
    rlht.commands.clear()
    log: list[str] = []
    c = Controller(
        EzoReader([]),
        [s, FakeActuator("fan", log)],
        polled=[s],
        ezo_period_s=2.0,
        clock=clock,
        sleep=clock.sleep,
    )
    events = c.stop_all()
    c.step()
    assert [(e.device, e.result) for e in events.result()] == [("heater", "ok"), ("fan", "ok")]
    assert ops(rlht) == [SET_SETPOINTS, SET_OPEN_DUTY]
    assert log == ["safe fan"]


# The poll


def tick(c: Controller, clock: FakeClock, seconds: float) -> None:
    for _ in range(round(seconds / 0.1)):
        c.step()
        clock.now = round(clock.now + 0.1, 9)


def test_the_poll_is_split_across_ticks_and_publishes_each_channel():
    rlht = FakeRlht()
    rlht.temperatures = [376, -32768]
    rlht.on_ms = [250, 0]
    s, clock = started(rlht)
    seen = []
    c = Controller(EzoReader([]), [s], polled=[s], ezo_period_s=2.0, clock=clock, sleep=clock.sleep)
    c.subscribe(seen.append)
    c.step()  # stages GET_STATE
    assert seen == [] and rlht.staged == 0x80
    clock.now += 0.1
    c.step()  # reads it
    [result] = seen
    assert (result.channel, result.outcome) == ("jacket", Outcome.OK)
    assert {v.field: (v.value, v.unit) for v in result.values} == {
        "temperature": (37.6, "°C"),
        "setpoint": (0.0, "°C"),
        "duty": (25.0, "%"),
    }


def test_a_missing_temperature_is_left_out():
    rlht = FakeRlht()
    second = ChannelConfig("trace", "Trace", output=2, tc=2)
    s, clock = started(rlht, device(JACKET, second))
    seen = []
    c = Controller(EzoReader([]), [s], polled=[s], ezo_period_s=2.0, clock=clock, sleep=clock.sleep)
    c.subscribe(seen.append)
    tick(c, clock, 0.2)
    trace = next(r for r in seen if r.channel == "trace")
    assert "temperature" not in {v.field for v in trace.values}


def test_the_poll_is_the_keep_alive_every_slice_poll_s():
    rlht = FakeRlht()
    s, clock = started(rlht)
    reads = []
    c = Controller(EzoReader([]), [s], polled=[s], ezo_period_s=2.0, clock=clock, sleep=clock.sleep)
    c.subscribe(lambda r: reads.append(clock.now))
    before = rlht.state_replies
    tick(c, clock, 5.0)
    # The firmware feeds its watchdog when it builds a reply: count those.
    assert rlht.state_replies - before == 5
    assert len(reads) == 5
    assert all(abs((b - a) - 1.0) < 1e-9 for a, b in zip(reads, reads[1:], strict=False))


def test_a_failed_poll_is_reported_on_each_channel_and_the_next_poll_goes_ahead():
    rlht = FakeRlht()
    s, clock = started(rlht)
    seen = []
    c = Controller(EzoReader([]), [s], polled=[s], ezo_period_s=2.0, clock=clock, sleep=clock.sleep)
    c.subscribe(seen.append)
    rlht.faults = ["corrupt"] * 3  # all three reads of one poll
    tick(c, clock, 2.0)
    assert [r.outcome for r in seen if isinstance(r, Result)] == [Outcome.ERROR, Outcome.OK]


# Opening slices from a config


def test_opened_slices_start_each_rlht_on_a_shared_bus_and_report_failures():
    good, other = FakeRlht(), FakeRlht(type_id=0x02)
    bus = FakeI2cBus({0x0A: good, 0x0B: other})
    opened: list[str] = []

    def open_bus(path: str) -> FakeI2cBus:
        opened.append(path)
        return bus

    heater = device()
    second = DeviceConfig(
        "second", "rlht", "/dev/i2c-1", 0x0B, channels=(ChannelConfig("lid", "Lid", 1, tc=1),)
    )
    motors = DeviceConfig(
        "motors", "dcmt", "/dev/i2c-1", 0x0C, channels=(ChannelConfig("stir", "Stir", 1),)
    )
    clock = FakeClock()
    with opened_slices(
        [heater, second, motors],
        watchdog_timeout_ms=5000,
        poll_s=1.0,
        open_bus=open_bus,
        clock=clock,
        sleep=clock.sleep,
    ) as (slices, reports):
        assert [s.name for s in slices] == ["heater"]
        assert opened == ["/dev/i2c-1"]
        problem = next(r for r in reports if isinstance(r, Result) and r.channel == "second")
        assert isinstance(problem, Result)
        assert problem.outcome is Outcome.ERROR and "expected type 0x01" in problem.detail
        assert any(getattr(r, "kind", None) == "slice-start" for r in reports)
        assert not bus.closed
    assert bus.closed
    assert other.commands == []


def test_a_slice_that_does_not_answer_is_a_problem_not_a_crash():
    clock = FakeClock()
    with opened_slices(
        [device()],
        watchdog_timeout_ms=5000,
        poll_s=1.0,
        open_bus=lambda p: FakeI2cBus(),
        clock=clock,
        sleep=clock.sleep,
    ) as (slices, reports):
        assert slices == []
        [problem] = reports
        assert isinstance(problem, Result) and problem.detail == "no answer on the bus"


def test_the_read_waits_for_the_reply_to_be_built():
    """CRUMBS asks for a pause after SET_REPLY before the read."""
    rlht = FakeRlht()
    s, clock = started(rlht)
    t = clock.now
    assert s.advance(t) == []  # stages GET_STATE
    assert s.advance(t + 0.005) == []  # too soon to read
    assert len(s.advance(t + 0.01)) == 1


def test_a_device_that_did_not_answer_at_start_up_is_asked_again():
    rlht = FakeRlht()
    rlht.faults = [OSError(errno.EREMOTEIO, "Remote I/O error")]
    s, _ = started(rlht)
    assert s.started and s.protected


def test_a_slice_that_identified_but_failed_start_up_still_gets_stop_all():
    rlht = FakeRlht(arms=False)
    clock = FakeClock()
    with opened_slices(
        [device()],
        watchdog_timeout_ms=5000,
        poll_s=1.0,
        open_bus=lambda path: FakeI2cBus({0x0A: rlht}),
        clock=clock,
        sleep=clock.sleep,
    ) as (slices, reports):
        [s] = slices
        assert s.identified and not s.started
        assert any(isinstance(r, Result) and "watchdog did not arm" in r.detail for r in reports)
        rlht.commands.clear()
        s.send_safe()
        assert ops(rlht) == [SET_SETPOINTS, SET_OPEN_DUTY]


def test_a_negative_temperature_reads_as_negative():
    rlht = FakeRlht()
    rlht.temperatures = [-123, -32768]
    s, clock = started(rlht)
    s.advance(clock.now)
    [result] = s.advance(clock.now + 0.1)
    assert isinstance(result, Result)
    assert {v.field: v.value for v in result.values}["temperature"] == -12.3


# Supervision (#24)

GET_WATCHDOG_EVERY = 5


def controller_for(
    s: RlhtSlice, clock: FakeClock, *others: FakeActuator
) -> tuple[Controller, list]:
    seen: list = []
    c = Controller(
        EzoReader([]), [s, *others], polled=[s], ezo_period_s=2.0, clock=clock, sleep=clock.sleep
    )
    c.subscribe(seen.append)
    return c, seen


def events(seen: list, kind: str) -> list:
    return [e for e in seen if getattr(e, "kind", None) == kind]


def test_the_watchdog_is_checked_after_every_fifth_poll():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, _ = controller_for(s, clock)
    before = rlht.watchdog_replies
    tick(c, clock, 10.0)
    assert rlht.state_replies >= 10
    assert rlht.watchdog_replies - before == 2


def test_a_trip_is_detected_from_the_trip_count_alone_and_re_asserted_in_order():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    s.set_setpoint(1, 370)
    rlht.trip_count += 1  # armed, not tripped now, but it tripped since
    rlht.commands.clear()
    checks = rlht.watchdog_replies
    tick(c, clock, 6.0)
    # The fifth poll's check finds the trip; a second check confirms the
    # re-assert in the next poll's cycle instead of five polls later.
    assert rlht.watchdog_replies - checks == 2
    [trip] = events(seen, "slice-trip")
    assert trip.result == "re-asserted" and "trips 0 -> 1" in trip.details
    assert ops(rlht)[:4] == [SET_MODE, SET_SETPOINTS, SET_TC, SET_WATCHDOG]
    assert dict(rlht.commands)[SET_SETPOINTS] == struct.pack("<hh", 370, 0)
    # The confirming check found it good: no second event.
    assert events(seen, "slice-trip") == [trip]


def test_a_tripped_flag_is_a_trip():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    rlht.tripped = 1
    tick(c, clock, 6.0)
    assert events(seen, "slice-trip")


def test_a_reboot_is_re_asserted_with_the_desired_setpoints():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    s.set_setpoint(1, 370)
    rlht.reboot()
    rlht.commands.clear()
    polls = rlht.state_replies
    tick(c, clock, 10.0)
    [event] = events(seen, "slice-reboot")
    assert event.result == "re-asserted"
    assert ops(rlht).count(SET_WATCHDOG) == 1 and rlht.armed == 1
    assert rlht.setpoints == [370, 0]
    # A poll every second throughout: the re-assert never pre-empts one.
    assert rlht.state_replies - polls >= 9


def test_a_count_below_the_baseline_while_armed_is_a_trip():
    # The u8 counter wrapping, or a build that arms its watchdog at boot.
    rlht = FakeRlht()
    rlht.trip_count = 255
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    rlht.trip()
    rlht.tripped = 0  # cleared since by a command (feastorg/Slice_RLHT#9)
    assert rlht.trip_count == 0
    tick(c, clock, 6.0)
    [trip] = events(seen, "slice-trip")
    assert trip.result == "re-asserted" and "trips 255 -> 0" in trip.details
    assert not events(seen, "slice-reboot")


def test_a_re_assert_that_does_not_take_is_reported_and_not_repeated_each_tick():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    rlht.reboot()
    rlht.arms = False  # it ignores SET_WATCHDOG from now on
    rlht.commands.clear()
    polls = rlht.state_replies
    tick(c, clock, 10.0)
    reboots = events(seen, "slice-reboot")
    assert reboots[0].result == "re-asserted"
    assert reboots[1].result == "error: the re-assert did not take"
    # Re-asserted at most once per scheduled check, never every tick.
    assert ops(rlht).count(SET_WATCHDOG) <= 3
    assert rlht.state_replies - polls >= 9
    # An e-stop is still seen while it goes on.
    rlht.flags = 0x01
    tick(c, clock, 2.0)
    assert [e.result for e in events(seen, "e-stop")] == ["held"]


def test_a_failed_re_assert_is_retried_at_the_next_check():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    rlht.trip()
    # SET_MODE goes through and clears the trip flag (feastorg/Slice_RLHT#9);
    # SET_SETPOINTS fails, so only the count still shows the trip.
    rlht.fail_opcodes[SET_SETPOINTS] = OSError(errno.EREMOTEIO, "Remote I/O error")
    tick(c, clock, 6.0)
    # Found at the fifth poll, and retried at the next one, not each tick.
    failed = events(seen, "slice-trip")
    assert len(failed) == 2
    assert all(e.result.startswith("error: re-assert failed") for e in failed)
    del rlht.fail_opcodes[SET_SETPOINTS]
    rlht.commands.clear()
    tick(c, clock, 2.0)
    assert events(seen, "slice-trip")[-1].result == "re-asserted"
    assert SET_WATCHDOG in ops(rlht)


def test_a_trip_after_stop_all_does_not_restart_heating():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    s.set_setpoint(1, 370)
    c.stop_all()
    tick(c, clock, 0.2)
    assert s.setpoints_deci == [0, 0]
    rlht.trip()
    rlht.commands.clear()
    tick(c, clock, 6.0)
    assert events(seen, "slice-trip")[-1].result == "re-asserted"
    assert all(
        payload == struct.pack("<hh", 0, 0) for op, payload in rlht.commands if op == SET_SETPOINTS
    )


def test_a_failed_watchdog_check_is_owed_to_the_next_poll():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    tick(c, clock, 3.5)
    rlht.corrupt_replies = {GET_WATCHDOG}
    before = rlht.watchdog_replies
    tick(c, clock, 1.2)
    assert rlht.watchdog_replies - before == READS_PER_POLL  # the check failed
    checks = rlht.watchdog_replies
    rlht.corrupt_replies = set()
    rlht.reboot()
    tick(c, clock, 1.0)
    # Retried after the next state poll, not five polls later, and not a
    # failed poll: the slice answered GET_STATE.
    assert rlht.watchdog_replies > checks
    assert events(seen, "slice-reboot")
    assert all(r.outcome == Outcome.OK for r in seen if isinstance(r, Result))


def test_watchdog_checks_that_keep_failing_are_reported_once_and_slow_down():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    rlht.corrupt_replies = {GET_WATCHDOG}
    polls = rlht.state_replies
    tick(c, clock, 8.0)  # checks at polls 5, 6 and 7 fail
    [unchecked] = events(seen, "slice-unchecked")
    assert unchecked.result == "error" and "3 watchdog checks failed" in unchecked.details
    assert not events(seen, "slice-unreachable")
    checks = rlht.watchdog_replies
    tick(c, clock, 10.0)
    # Back to every fifth poll: two checks of three attempts each.
    assert rlht.watchdog_replies - checks == 2 * READS_PER_POLL
    assert events(seen, "slice-unchecked") == [unchecked]
    assert rlht.state_replies - polls >= 17
    # Once a check answers, a reboot is seen again.
    rlht.corrupt_replies = set()
    rlht.reboot()
    tick(c, clock, 5.0)
    assert events(seen, "slice-reboot")


def test_a_reboot_while_unreachable_is_re_asserted_even_with_a_confirm_pending():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    rlht.trip()
    tick(c, clock, 4.5)  # the fifth poll re-asserts; its confirm is pending
    assert events(seen, "slice-trip")[-1].result == "re-asserted"
    rlht.faults = [OSError(errno.EREMOTEIO, "Remote I/O error")] * 9
    tick(c, clock, 3.0)
    assert s.unreachable
    rlht.reboot()  # it lost power while it did not answer
    tick(c, clock, 2.0)
    [reboot] = events(seen, "slice-reboot")
    assert reboot.result == "re-asserted" and rlht.armed == 1


def test_a_reboot_after_failed_checks_is_re_asserted_even_with_a_confirm_pending():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    rlht.trip()
    tick(c, clock, 4.5)  # the fifth poll re-asserts; its confirm is pending
    assert events(seen, "slice-trip")[-1].result == "re-asserted"
    rlht.corrupt_replies = {GET_WATCHDOG}
    for _ in range(50):  # until the confirm and the next two checks fail
        tick(c, clock, 0.1)
        if events(seen, "slice-unchecked"):
            break
    assert events(seen, "slice-unchecked")
    rlht.corrupt_replies = set()
    rlht.reboot()
    tick(c, clock, 5.0)  # the next scheduled check answers
    [reboot] = events(seen, "slice-reboot")
    assert reboot.result == "re-asserted" and rlht.armed == 1


def test_a_check_that_answers_starts_the_count_of_failed_checks_again():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    tick(c, clock, 3.5)
    rlht.corrupt_replies = {GET_WATCHDOG}
    tick(c, clock, 2.0)  # the checks at polls 5 and 6 fail
    rlht.corrupt_replies = set()
    tick(c, clock, 1.0)  # poll 7's check answers
    rlht.corrupt_replies = {GET_WATCHDOG}
    checks = rlht.watchdog_replies
    tick(c, clock, 4.5)  # the checks at polls 10 and 11 fail
    assert rlht.watchdog_replies - checks == 2 * READS_PER_POLL
    assert not events(seen, "slice-unchecked")


def test_a_slice_silent_while_a_check_is_owed_is_asked_three_times_a_poll():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    tick(c, clock, 3.5)
    rlht.corrupt_replies = {GET_WATCHDOG}
    before = rlht.watchdog_replies
    tick(c, clock, 1.2)
    assert rlht.watchdog_replies - before == READS_PER_POLL  # a check is now owed
    rlht.faults = [OSError(errno.EREMOTEIO, "Remote I/O error")] * 1000
    tick(c, clock, 5.0)
    assert 1000 - len(rlht.faults) <= READS_PER_POLL * 6
    assert events(seen, "slice-unreachable") and s.unreachable


def test_a_silent_slice_is_asked_three_times_a_poll_and_goes_unreachable():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    tick(c, clock, 4.5)
    rlht.faults = [OSError(errno.EREMOTEIO, "Remote I/O error")] * 1000
    tick(c, clock, 5.0)
    asked = 1000 - len(rlht.faults)
    assert asked <= READS_PER_POLL * 6
    [down] = events(seen, "slice-unreachable")
    assert s.unreachable


def test_a_slice_without_a_watchdog_is_never_checked():
    rlht = FakeRlht(caps=0x3F)
    s, clock = started(rlht, device(allow_unprotected=True))
    c, _ = controller_for(s, clock)
    tick(c, clock, 10.0)
    assert rlht.watchdog_replies == 0


def test_an_e_stop_sends_stop_all_and_nothing_resumes():
    rlht = FakeRlht()
    s, clock = started(rlht)
    log: list[str] = []
    c, seen = controller_for(s, clock, FakeActuator("fan", log))
    s.set_setpoint(1, 370)
    rlht.flags = 0x01  # e-stop pressed on the slice
    rlht.commands.clear()
    tick(c, clock, 3.0)
    [held] = events(seen, "e-stop")
    assert held.result == "held"
    # Stop-all reached every actuator, and the safe state goes every poll.
    assert log == ["safe fan"]
    assert ops(rlht).count(SET_SETPOINTS) >= 2
    assert s.setpoints_deci == [0, 0]
    rlht.flags = 0
    rlht.commands.clear()
    tick(c, clock, 2.0)
    released = events(seen, "e-stop")[-1]
    assert released.result == "released"
    # Nothing was sent to restart heating.
    assert all(
        payload == struct.pack("<hh", 0, 0) for op, payload in rlht.commands if op == SET_SETPOINTS
    )


def test_a_re_assert_during_an_e_stop_keeps_setpoints_at_zero():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    rlht.flags = 0x01
    tick(c, clock, 1.0)
    # An operator setting a setpoint while the e-stop is held (#24 part 3)
    # must not reach the slice.
    s.setpoints_deci = [370, 0]
    rlht.trip_count += 1
    rlht.commands.clear()
    tick(c, clock, 6.0)
    assert events(seen, "slice-trip")
    assert all(
        payload == struct.pack("<hh", 0, 0) for op, payload in rlht.commands if op == SET_SETPOINTS
    )


def test_a_failed_read_is_retried_twice_within_the_poll():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    rlht.faults = ["corrupt", "corrupt"]
    tick(c, clock, 1.0)
    assert [r.outcome for r in seen if isinstance(r, Result)] == [Outcome.OK]


def test_three_failed_polls_make_the_slice_unreachable_and_an_answer_brings_it_back():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    nack = OSError(errno.EREMOTEIO, "Remote I/O error")
    # Each failed attempt takes one fault; three attempts make a failed poll.
    rlht.faults = [nack] * 6  # two failed polls, then one that answers
    tick(c, clock, 3.0)
    assert not s.unreachable
    rlht.faults = [nack] * 9  # three failed polls in a row
    tick(c, clock, 3.0)
    [down] = events(seen, "slice-unreachable")
    assert s.unreachable and "3 polls failed: no answer on the bus" in down.details
    before = rlht.watchdog_replies
    tick(c, clock, 2.0)
    assert events(seen, "slice-reachable") and not s.unreachable
    # It may have restarted while silent: the watchdog is checked at once.
    assert rlht.watchdog_replies > before


# Setpoints


def test_a_setpoint_is_sent_at_once_and_kept_for_a_re_assert():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    rlht.commands.clear()
    s.set_setpoint(1, 370)
    s.set_setpoint(2, 450)
    assert rlht.commands == [
        (SET_SETPOINTS, struct.pack("<hh", 370, 0)),
        (SET_SETPOINTS, struct.pack("<hh", 370, 450)),
    ]
    rlht.reboot()
    tick(c, clock, 6.0)
    assert events(seen, "slice-reboot")[-1].result == "re-asserted"
    assert rlht.setpoints == [370, 450]


def test_a_setpoint_that_is_not_sent_is_not_kept():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, _ = controller_for(s, clock)
    rlht.fail_opcodes[SET_SETPOINTS] = OSError(errno.EREMOTEIO, "Remote I/O error")
    with pytest.raises(SliceError, match="no answer on the bus"):
        s.set_setpoint(1, 370)
    del rlht.fail_opcodes[SET_SETPOINTS]
    rlht.reboot()
    tick(c, clock, 6.0)
    # The re-assert sends what was last set successfully, not the failure.
    assert rlht.setpoints == [0, 0]


def test_a_setpoint_is_refused_while_the_e_stop_is_held():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, _ = controller_for(s, clock)
    rlht.flags = 0x01
    tick(c, clock, 1.5)
    rlht.commands.clear()
    with pytest.raises(SliceBusy, match="e-stop on heater is held"):
        s.set_setpoint(1, 370)
    assert SET_SETPOINTS not in ops(rlht) and s.setpoints_deci == [0, 0]


def test_a_setpoint_is_refused_by_a_slice_that_cannot_take_one():
    read_only, _ = started(FakeRlht(caps=0x3F))
    assert read_only.read_only
    with pytest.raises(SliceBusy, match="read-only"):
        read_only.set_setpoint(1, 370)
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, _ = controller_for(s, clock)
    rlht.faults = [OSError(errno.EREMOTEIO, "Remote I/O error")] * 9
    tick(c, clock, 3.0)
    assert s.unreachable
    with pytest.raises(SliceError, match="unreachable"):
        s.set_setpoint(1, 370)
    clock = FakeClock()
    never_started = RlhtSlice(
        device(),
        CrumbsPort(FakeI2cBus({0x0A: FakeRlht()}), 0x0A, sleep=clock.sleep),
        watchdog_timeout_ms=5000,
        poll_s=1.0,
        clock=clock,
        sleep=clock.sleep,
    )
    with pytest.raises(SliceError, match="did not finish start-up"):
        never_started.set_setpoint(1, 370)


def test_setpoints_lost_with_no_trip_or_reboot_are_dropped_not_resumed():
    # An e-stop pressed and released between two polls: the firmware zeroes
    # the setpoints on the press, and GET_STATE never shows the flag.
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    s.set_setpoint(1, 400)
    rlht.setpoints = [0, 0]
    tick(c, clock, 1.5)
    [changed] = events(seen, "slice-setpoints-changed")
    assert changed.result == "safe state sent"
    assert "ran 0 and 0 °C, not 40 and 0 °C" in changed.details
    assert s.setpoints_deci == [0, 0]
    # So a trip afterwards, found at the scheduled check, re-asserts nothing
    # that heats.
    rlht.trip()
    tick(c, clock, 6.0)
    assert events(seen, "slice-trip")[-1].result == "re-asserted"
    assert rlht.setpoints == [0, 0]
    # A later change is a new one, and told again.
    s.set_setpoint(1, 300)
    rlht.setpoints = [0, 0]
    tick(c, clock, 1.5)
    assert len(events(seen, "slice-setpoints-changed")) == 2


def test_setpoints_a_trip_zeroed_are_re_asserted_at_the_next_poll():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    s.set_setpoint(1, 400)
    rlht.trip()  # zeroes the slice's setpoints, as watchdogLogic() does
    tick(c, clock, 1.5)
    # Checked at once, not at the fifth poll, and re-asserted.
    assert events(seen, "slice-trip")[-1].result == "re-asserted"
    assert not events(seen, "slice-setpoints-changed")
    assert rlht.setpoints == [400, 0] and s.setpoints_deci == [400, 0]


def test_a_setpoint_that_landed_although_its_send_failed_is_stopped():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    rlht.fail_after[SET_SETPOINTS] = OSError(errno.ETIMEDOUT, "Connection timed out")
    with pytest.raises(SendFailed):
        s.set_setpoint(1, 700)
    del rlht.fail_after[SET_SETPOINTS]
    assert rlht.setpoints == [700, 0] and s.setpoints_deci == [0, 0]
    tick(c, clock, 1.5)
    assert events(seen, "slice-setpoints-changed")
    assert rlht.setpoints == [0, 0]


def test_an_unprotected_slice_drops_changed_setpoints_without_a_check():
    rlht = FakeRlht(caps=0x3F)
    s, clock = started(rlht, device(allow_unprotected=True))
    c, seen = controller_for(s, clock)
    s.set_setpoint(1, 400)
    rlht.setpoints = [0, 0]
    tick(c, clock, 1.0)
    assert events(seen, "slice-setpoints-changed")
    assert rlht.watchdog_replies == 0


def test_a_read_only_slice_running_its_own_setpoints_is_left_alone():
    rlht = FakeRlht(caps=0x3F)
    s, clock = started(rlht)
    assert s.read_only
    c, seen = controller_for(s, clock)
    rlht.setpoints = [500, 0]  # set by something else
    rlht.commands.clear()
    tick(c, clock, 3.0)
    assert not events(seen, "slice-setpoints-changed") and rlht.commands == []


def test_a_setpoint_set_at_any_tick_is_never_taken_for_drift():
    # Set between a GET_STATE and the GET_WATCHDOG after it, a setpoint must
    # not be judged against that older GET_STATE.
    for offset in range(100):
        rlht = FakeRlht()
        s, clock = started(rlht)
        c, seen = controller_for(s, clock)
        tick(c, clock, offset / 10)
        c.call(lambda s=s: s.set_setpoint(1, 400))
        tick(c, clock, 3.0)
        assert not events(seen, "slice-setpoints-changed"), f"dropped at tick {offset}"
        assert rlht.setpoints == [400, 0] and s.setpoints_deci == [400, 0]


def test_a_drop_that_keeps_failing_is_retried_each_poll_but_told_once():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    rlht.fail_opcodes[SET_SETPOINTS] = OSError(errno.EIO, "I/O error")
    rlht.setpoints = [500, 0]  # set by something else
    tick(c, clock, 10.0)
    [failed] = events(seen, "slice-setpoints-changed")
    assert failed.result.startswith("error: safe state not sent")
    assert ops(rlht).count(SET_OPEN_DUTY) >= 8  # the safe state, tried each poll
    del rlht.fail_opcodes[SET_SETPOINTS]
    tick(c, clock, 3.0)
    assert [e.result for e in events(seen, "slice-setpoints-changed")] == [
        failed.result,
        "safe state sent",
    ]
    assert rlht.setpoints == [0, 0]


def test_a_setpoint_is_refused_while_a_drift_is_being_checked():
    # After an e-stop tap no poll saw, a setpoint for one output must not
    # restart the other: it waits for the check.
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, seen = controller_for(s, clock)
    s.set_setpoint(1, 400)
    s.set_setpoint(2, 300)
    rlht.setpoints = [0, 0]
    refused = False
    for _ in range(20):
        tick(c, clock, 0.1)
        try:
            s.set_setpoint(1, 410)
        except SliceBusy as e:
            assert "being checked" in str(e)
            refused = True
            break
        rlht.setpoints = [0, 0]  # accepted before the drift was seen: tap again
    assert refused
    tick(c, clock, 1.0)
    assert events(seen, "slice-setpoints-changed")
    s.set_setpoint(1, 410)
    assert rlht.setpoints == [410, 0]


def test_with_checks_failing_a_setpoint_is_not_told_a_check_is_pending():
    rlht = FakeRlht()
    s, clock = started(rlht)
    c, _ = controller_for(s, clock)
    rlht.corrupt_replies = {GET_WATCHDOG}
    rlht.fail_opcodes[SET_SETPOINTS] = OSError(errno.EIO, "I/O error")
    rlht.setpoints = [500, 0]  # set by something else; the safe state fails
    tick(c, clock, 10.0)
    # The send is what fails, and that is what the operator is told.
    with pytest.raises(SendFailed):
        s.set_setpoint(1, 400)


def test_a_slice_that_did_not_start_says_so_in_its_status():
    clock = FakeClock()
    never_started = RlhtSlice(
        device(),
        CrumbsPort(FakeI2cBus({0x0A: FakeRlht()}), 0x0A, sleep=clock.sleep),
        watchdog_timeout_ms=5000,
        poll_s=1.0,
        clock=clock,
        sleep=clock.sleep,
    )
    assert never_started.status().state == "did not finish start-up"
    assert never_started.status("error: no answer").state == "error: no answer"
