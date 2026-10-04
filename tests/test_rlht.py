import errno
import struct

import pytest
from fakes import FakeActuator, FakeClock, FakeI2cBus, FakeRlht

from openreactor.config import ChannelConfig, DeviceConfig
from openreactor.controller import Controller
from openreactor.ezo import EzoReader, Outcome, Result
from openreactor.rlht import CrumbsPort, RlhtSlice, SliceError, opened_slices

SET_SETPOINTS, SET_OPEN_DUTY, SET_MODE, SET_PID, SET_PERIODS, SET_TC = (
    0x02,
    0x06,
    0x01,
    0x03,
    0x04,
    0x05,
)
SET_WATCHDOG = 0x7E
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
        SET_TC,
        SET_PERIODS,
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
    rlht.faults = ["corrupt"]
    tick(c, clock, 2.0)
    assert [r.outcome for r in seen] == [Outcome.ERROR, Outcome.OK]


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
    assert {v.field: v.value for v in result.values}["temperature"] == -12.3
