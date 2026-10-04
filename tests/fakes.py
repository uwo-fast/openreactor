"""Test doubles at the hardware boundary. Nothing here ships."""

from __future__ import annotations

from collections import deque

from openreactor.ezo import EzoStatusError, Outcome, Value


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakePort:
    """One EZO circuit. ``replies`` scripts what successive reads return: a
    tuple of values, or an ``Outcome`` the circuit reports instead."""

    def __init__(
        self,
        family: str,
        values: tuple[Value, ...] = (),
        *,
        reports: str | None = None,
        config: int | None = None,
        wait_ms: int = 900,
        trace: list[tuple[str, float]] | None = None,
        clock: FakeClock | None = None,
    ):
        self.family = family
        self.reports = reports if reports is not None else family
        self.values = values
        self.config = config
        self.wait_ms = wait_ms
        self.replies: deque[tuple[Value, ...] | Outcome] = deque()
        self.sent: list[str] = []
        self.read_temperatures: list[float | None] = []
        self.configs_used: list[int | None] = []
        self.calibration = "not calibrated"
        self.ack: Outcome = Outcome.OK
        self.closed = False
        # Shared across ports to check ordering and timing: (event, time).
        self.trace = trace
        self.clock = clock
        self.send_error: Exception | None = None
        self.family_error: Exception | None = None

    def _note(self, event: str) -> None:
        if self.trace is not None:
            self.trace.append((f"{event} {self.family}", self.clock() if self.clock else 0.0))

    def send_info_query(self) -> int:
        self.sent.append("info")
        return 300

    def read_family(self) -> str:
        if self.family_error is not None:
            raise self.family_error
        return self.reports

    def send_config_query(self) -> int:
        self.sent.append("config")
        return 300

    def read_config(self) -> int | None:
        return self.config

    def send_read(self, temperature_c: float | None) -> int:
        if self.send_error is not None:
            raise self.send_error
        self._note("send")
        self.sent.append("read")
        self.read_temperatures.append(temperature_c)
        return self.wait_ms

    def read_values(self, config: int | None) -> tuple[Value, ...]:
        self._note("read")
        self.configs_used.append(config)
        reply = self.replies.popleft() if self.replies else self.values
        if isinstance(reply, Outcome):
            raise EzoStatusError(reply)
        return reply

    def send_temperature(self, temperature_c: float) -> int:
        self.sent.append(f"temperature {temperature_c}")
        return 300

    def send_calibration_query(self) -> int:
        self.sent.append("cal?")
        return 300

    def read_calibration_status(self) -> str:
        return self.calibration

    def send_calibration_clear(self) -> int:
        self.sent.append("cal clear")
        self.calibration = "not calibrated"
        return 300

    def send_calibration(self, point: str, value: float | None) -> int:
        self.sent.append(f"cal {point} {value}")
        self.calibration = f"calibrated at {point}"
        return 900

    def read_ack(self) -> None:
        if self.ack is not Outcome.OK:
            raise EzoStatusError(self.ack)

    def close(self) -> None:
        self.closed = True
