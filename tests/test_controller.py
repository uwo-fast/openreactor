import threading

import pytest
from fakes import FakeActuator, FakeClock, FakePort

from openreactor.config import DeviceConfig
from openreactor.controller import TICK_S, Controller, ControllerClosed, Event
from openreactor.ezo import (
    FAMILIES,
    EzoChannel,
    EzoReader,
    Outcome,
    Result,
    Value,
    calibrate_steps,
    calibration_status_steps,
)

PH = (Value("", 7.0, "pH"),)
ORP = (Value("", 225.0, "mV"),)


def setup(*ports: tuple[str, str, FakePort], actuators=(), auto_read=False):
    clock = FakeClock()
    channels = [
        EzoChannel(DeviceConfig(n, k, "/dev/i2c-1", 0x60 + i), p)
        for i, (n, k, p) in enumerate(ports)
    ]
    reader = EzoReader(channels, clock=clock, sleep=clock.sleep)
    controller = Controller(
        reader,
        actuators,
        ezo_period_s=2.0,
        auto_read=auto_read,
        clock=clock,
        sleep=clock.sleep,
        wall=lambda: 1_700_000_000.0,
    )
    return controller, clock


def tick(controller: Controller, clock: FakeClock, n: int = 1) -> None:
    for _ in range(n):
        controller.step()
        clock.now = round(clock.now + TICK_S, 9)


# Stop-all


def test_stop_all_is_handled_on_the_next_tick():
    log: list[str] = []
    c, clock = setup(actuators=[FakeActuator("heater", log), FakeActuator("motors", log)])

    future = c.stop_all()
    assert not future.done()
    tick(c, clock)

    assert future.done()
    assert log == ["safe heater", "safe motors"]
    assert [(e.kind, e.device, e.result) for e in future.result()] == [
        ("stop-all", "heater", "ok"),
        ("stop-all", "motors", "ok"),
    ]


def test_stop_all_tries_every_actuator_and_reports_failures():
    log: list[str] = []
    c, clock = setup(
        actuators=[FakeActuator("heater", log, fail=OSError("no ack")), FakeActuator("motors", log)]
    )
    future = c.stop_all("system")
    tick(c, clock)

    assert log == ["safe motors"]
    assert [(e.source, e.device, e.result) for e in future.result()] == [
        ("system", "heater", "error: no ack"),
        ("system", "motors", "ok"),
    ]


def test_stop_all_events_reach_subscribers():
    seen: list[Result | Event] = []
    c, clock = setup(actuators=[FakeActuator("heater")])
    c.subscribe(seen.append)
    c.stop_all()
    tick(c, clock)
    assert [(e.kind, e.device) for e in seen if isinstance(e, Event)] == [("stop-all", "heater")]


def test_an_ezo_read_in_progress_never_delays_the_tick():
    trace: list[tuple[str, float]] = []
    clock = FakeClock()
    port = FakePort("ph", PH, wait_ms=900, trace=trace, clock=clock)
    reader = EzoReader(
        [EzoChannel(DeviceConfig("ph", "ezo-ph", "/dev/i2c-1", 0x63), port)], clock=clock
    )
    log: list[str] = []
    c = Controller(
        reader, [FakeActuator("heater", log)], ezo_period_s=2.0, clock=clock, sleep=clock.sleep
    )

    read = c.read_cycle()
    tick(c, clock, 3)  # the read is sent and pending
    stop = c.stop_all()
    tick(c, clock)

    # Stop-all ran on the very next tick while the pH read was still pending,
    # and no tick slept or touched the circuit before its delay was up.
    assert stop.done() and log == ["safe heater"]
    assert not read.done()
    assert clock.sleeps == []
    assert trace == [("send ph", 0.0)]
    tick(c, clock, 6)
    assert read.done()
    assert trace[-1] == ("read ph", pytest.approx(0.9))


# Reads and jobs


def test_auto_read_starts_a_cycle_every_period_and_publishes_results():
    port = FakePort("ph", PH, wait_ms=900)
    c, clock = setup(("ph", "ezo-ph", port), auto_read=True)
    results: list[Result | Event] = []
    c.subscribe(results.append)

    tick(c, clock, 61)  # 6.1 s: cycles at 0, 2, 4 and 6 s

    assert port.sent.count("read") == 4
    assert [r.outcome for r in results if isinstance(r, Result)] == [Outcome.OK] * 3


def test_a_job_waits_for_a_pending_read_and_reads_skip_a_busy_circuit():
    port = FakePort("ec")
    c, clock = setup(("ec", "ezo-ec", port))

    read = c.read_cycle()
    tick(c, clock)  # read sent
    job = c.run_job("ec", calibrate_steps(FAMILIES["ezo-ec"], port, "dry", None))
    tick(c, clock, 3)
    assert not any(s.startswith(("temperature", "cal")) for s in port.sent)

    tick(c, clock, 7)  # read collected at 0.9 s, then the job starts
    assert read.done()
    assert port.sent[-1] == "temperature 25.0"

    second = c.read_cycle()
    tick(c, clock)
    assert second.done() and second.result() == []  # skipped: the job is running
    assert port.sent.count("read") == 1

    tick(c, clock, 20)
    assert job.done() and job.exception() is None
    assert port.sent[-1] == "cal dry None"


def test_a_job_returns_its_value_and_raises_its_error():
    port = FakePort("ph")
    port.calibration = "two point"
    c, clock = setup(("ph", "ezo-ph", port))

    status = c.run_job("ph", calibration_status_steps(port))
    bad = c.run_job("ph", calibrate_steps(FAMILIES["ezo-ph"], port, "dry", None))
    tick(c, clock, 10)

    assert status.result() == "two point"
    with pytest.raises(ValueError, match="no calibration point 'dry'"):
        bad.result()
    assert port.sent == ["cal?"]


def test_jobs_on_one_circuit_run_one_at_a_time():
    port = FakePort("ph")
    c, clock = setup(("ph", "ezo-ph", port))
    first = c.run_job("ph", calibrate_steps(FAMILIES["ezo-ph"], port, "mid", 7.0))
    second = c.run_job("ph", calibrate_steps(FAMILIES["ezo-ph"], port, "low", 4.0))

    tick(c, clock, 5)
    assert port.sent == ["cal mid 7.0"]  # the second waits for the first
    tick(c, clock, 20)
    assert first.done() and second.done()
    assert port.sent == ["cal mid 7.0", "cal low 4.0"]


# Ticking


def test_run_ticks_every_100_ms_without_drift():
    c, clock = setup()
    starts: list[float] = []

    def step() -> None:
        starts.append(clock.now)
        clock.now += 0.03  # each tick takes 30 ms

    c.step = step  # type: ignore[method-assign]
    c.run(lambda: len(starts) >= 50)
    # Every tick starts on a 100 ms boundary: the time spent in a tick is
    # taken out of the sleep, not added to it.
    assert starts == [pytest.approx(i * TICK_S) for i in range(50)]


def test_an_overrunning_tick_is_not_made_up():
    c, clock = setup()
    slow = {"n": 0}

    def step() -> None:
        slow["n"] += 1
        if slow["n"] == 3:
            clock.now += 0.25  # this tick takes 250 ms

    c.step = step  # type: ignore[method-assign]
    c.run(lambda: slow["n"] >= 5)
    # Ticks at 0, 0.1 and 0.2, which overruns to 0.45; the next tick is on
    # the 0.5 boundary, not a catch-up at 0.45, then 0.6, then the loop ends.
    assert clock.sleeps[2] == pytest.approx(0.05)
    assert clock.now == pytest.approx(0.7)


def test_close_sends_stop_all_on_the_calling_thread():
    log: list[str] = []
    c, _ = setup(actuators=[FakeActuator("heater", log)])
    events = c.close()
    assert log == ["safe heater"]
    assert [e.source for e in events] == ["system"]


def test_the_controller_thread_handles_stop_all_and_closes():
    log: list[str] = []
    reader = EzoReader([])
    c = Controller(reader, [FakeActuator("heater", log)], ezo_period_s=2.0)
    c.start()
    try:
        assert [e.result for e in c.stop_all().result(timeout=2)] == ["ok"]
    finally:
        events = c.close(timeout_s=2)
    assert log == ["safe heater", "safe heater"]
    assert [e.source for e in events] == ["system"]
    assert not any(t.name == "openreactor-controller" for t in threading.enumerate())


# Review fixes: stop-all must be fast and certain


def test_a_failing_listener_does_not_cut_stop_all_short():
    log: list[str] = []
    c, clock = setup(actuators=[FakeActuator("heater", log), FakeActuator("motors", log)])

    def broken(item: Result | Event) -> None:
        raise BrokenPipeError

    c.subscribe(broken)
    future = c.stop_all()
    tick(c, clock)

    assert log == ["safe heater", "safe motors"]
    assert [e.result for e in future.result()] == ["ok", "ok"]


def test_an_interrupt_during_stop_all_still_makes_every_actuator_safe():
    log: list[str] = []
    c, _ = setup(
        actuators=[
            FakeActuator("heater", log, fail=KeyboardInterrupt()),
            FakeActuator("motors", log),
        ]
    )
    with pytest.raises(KeyboardInterrupt):
        c.close()
    assert log == ["safe motors"]


def test_close_cancels_a_half_finished_calibration():
    port = FakePort("ec")
    c, clock = setup(("ec", "ezo-ec", port))
    job = c.run_job("ec", calibrate_steps(FAMILIES["ezo-ec"], port, "dry", None))
    read = c.read_cycle()
    tick(c, clock)  # the job starts: the temperature reset is sent
    assert port.sent == ["temperature 25.0"]

    clock.now += 5.0  # long past the step's delay
    c.close()

    assert port.sent == ["temperature 25.0"]  # no Cal after the close
    assert isinstance(job.exception(), ControllerClosed)
    assert read.done()


def test_close_twice_does_nothing_the_second_time():
    log: list[str] = []
    c, _ = setup(actuators=[FakeActuator("heater", log)])
    assert len(c.close()) == 1
    assert c.close() == []
    assert log == ["safe heater"]


def test_a_read_cycle_completes_while_auto_reads_keep_circuits_busy():
    # The period is shorter than the read delay, so an auto read is always
    # in flight; the request completes on the reads it joined.
    a, b = FakePort("ph", PH, wait_ms=900), FakePort("orp", ORP, wait_ms=900)
    clock = FakeClock()
    channels = [
        EzoChannel(DeviceConfig("ph", "ezo-ph", "/dev/i2c-1", 0x63), a),
        EzoChannel(DeviceConfig("orp", "ezo-orp", "/dev/i2c-1", 0x62), b),
    ]
    reader = EzoReader(channels, clock=clock, sleep=clock.sleep)
    c = Controller(reader, ezo_period_s=0.8, auto_read=True, clock=clock, sleep=clock.sleep)
    for offset in range(20):
        future = c.read_cycle()
        tick(c, clock, offset % 7 + 1)
        tick(c, clock, 30)
        assert future.done(), offset
        assert sorted(r.channel for r in future.result()) == ["orp", "ph"]


def test_wait_refuses_while_the_controller_has_its_own_thread():
    c = Controller(EzoReader([]), ezo_period_s=2.0)
    c.start()
    try:
        with pytest.raises(RuntimeError, match="own thread"):
            c.wait(c.stop_all())
        with pytest.raises(RuntimeError, match="already running"):
            c.start()
    finally:
        c.close(timeout_s=2)


def test_a_failing_tick_does_not_kill_the_controller_thread():
    log: list[str] = []
    reader = EzoReader([])
    calls = {"n": 0}
    original = reader.collect

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("bus glitch")
        return original()

    reader.collect = flaky  # type: ignore[method-assign]
    c = Controller(reader, [FakeActuator("heater", log)], ezo_period_s=2.0)
    c.start()
    try:
        assert [e.result for e in c.stop_all().result(timeout=2)] == ["ok"]
        assert calls["n"] >= 2
    finally:
        c.close(timeout_s=2)
    assert log.count("safe heater") == 2


def test_close_gives_up_on_a_hung_actuator_without_hanging_the_process():
    release = threading.Event()

    class Hung:
        name = "heater"

        def send_safe(self) -> None:
            release.wait(5)

    c = Controller(EzoReader([]), [Hung()], ezo_period_s=2.0)
    c.start()
    thread = next(t for t in threading.enumerate() if t.name == "openreactor-controller")
    try:
        with pytest.raises(TimeoutError):
            c.close(timeout_s=0.3)
        # Still stuck in the actuator, but a daemon: it cannot keep the
        # process alive.
        assert thread.is_alive() and thread.daemon
    finally:
        release.set()


def test_a_job_cancelled_before_it_starts_never_touches_its_circuit():
    port = FakePort("ph")
    log: list[str] = []
    c, clock = setup(("ph", "ezo-ph", port), actuators=[FakeActuator("heater", log)])
    job = c.run_job("ph", calibrate_steps(FAMILIES["ezo-ph"], port, "mid", 7.0))
    cycle = c.read_cycle()
    assert job.cancel() and cycle.cancel()

    tick(c, clock, 15)

    assert port.sent == []
    assert c.close()[0].result == "ok"
    assert log == ["safe heater"]


def test_close_sends_stop_all_with_cancelled_work_outstanding():
    port = FakePort("ph")
    log: list[str] = []
    c, clock = setup(("ph", "ezo-ph", port), actuators=[FakeActuator("heater", log)])
    started = c.run_job("ph", calibrate_steps(FAMILIES["ezo-ph"], port, "mid", 7.0))
    tick(c, clock)  # the job is running and can no longer be cancelled
    queued = c.run_job("ph", calibrate_steps(FAMILIES["ezo-ph"], port, "low", 4.0))
    assert not started.cancel()
    assert queued.cancel()

    events = c.close()

    assert [e.result for e in events] == ["ok"] and log == ["safe heater"]
    assert isinstance(started.exception(), ControllerClosed)
    assert queued.cancelled()


def test_a_threaded_close_sends_stop_all_after_a_caller_cancels():
    log: list[str] = []
    c = Controller(EzoReader([]), [FakeActuator("heater", log)], ezo_period_s=2.0)
    c.start()
    try:
        never = c.run_job("ph", calibration_status_steps(FakePort("ph")))
        never.cancel()
    finally:
        events = c.close(timeout_s=2)
    assert [e.result for e in events] == ["ok"]
    assert log == ["safe heater"]


def test_overlapping_read_cycles_each_get_one_result_per_circuit():
    port = FakePort("ph", PH)
    c, clock = setup(("ph", "ezo-ph", port))
    first = c.read_cycle()
    tick(c, clock, 2)
    second = c.read_cycle()  # joins the read already in flight
    tick(c, clock, 15)
    assert [r.channel for r in first.result()] == ["ph"]
    assert [r.channel for r in second.result()] == ["ph"]
    assert port.sent.count("read") == 1


def test_a_cancelled_stop_all_still_makes_safe_and_reports():
    log: list[str] = []
    seen: list[Result | Event] = []
    c, clock = setup(actuators=[FakeActuator("heater", log)])
    c.subscribe(seen.append)
    future = c.stop_all()
    assert future.cancel()

    tick(c, clock)
    events = c.close()

    assert log == ["safe heater", "safe heater"]
    assert [e.source for e in seen if isinstance(e, Event)] == ["user", "system"]
    assert [e.result for e in events] == ["ok"]
