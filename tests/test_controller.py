import threading

import pytest
from fakes import FakeActuator, FakeClock, FakePort

from openreactor.config import DeviceConfig
from openreactor.controller import TICK_S, Controller, Event
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
    stop_at = 50
    c.run(lambda: len(clock.sleeps) >= stop_at)
    assert clock.now == pytest.approx(stop_at * TICK_S)
    assert all(s == pytest.approx(TICK_S) for s in clock.sleeps)


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
