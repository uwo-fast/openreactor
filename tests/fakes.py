"""Test doubles at the hardware boundary. Nothing here ships."""

from __future__ import annotations

import errno
import struct
from collections import deque

from crumbs_i2c import Message, decode, encode

from openreactor.ezo import EzoStatusError, Outcome, Value

# RLHT and CRUMBS opcodes, written out here rather than imported: the tests
# hold the client to these values, so they must not come from its source.
GET_VERSION = 0x00
SET_MODE = 0x01
SET_SETPOINTS = 0x02
SET_PID = 0x03
SET_PERIODS = 0x04
SET_TC = 0x05
SET_OPEN_DUTY = 0x06
GET_WATCHDOG = 0x7D
SET_WATCHDOG = 0x7E
GET_CAPS = 0x7F
GET_STATE = 0x80
SET_REPLY = 0xFE


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


class FakeActuator:
    """An output that records each safe-state command."""

    def __init__(self, name: str, log: list[str] | None = None, fail: BaseException | None = None):
        self.name = name
        self.log = log if log is not None else []
        self.fail = fail

    def send_safe(self) -> None:
        if self.fail is not None:
            raise self.fail
        self.log.append(f"safe {self.name}")


class FakeRlht:
    """An RLHT slice as its CRUMBS contract describes it: SET_REPLY stages a
    reply, reads return the staged reply padded to the read size, SETs change
    its state. ``commands`` records each SET as (opcode, payload)."""

    def __init__(
        self,
        *,
        type_id: int = 0x01,
        crumbs_version: int = 1500,
        module: tuple[int, int, int] = (1, 0, 0),
        caps: int = 0x7F,  # the six baseline controls and the watchdog
        arms: bool = True,
    ):
        self.type_id = type_id
        self.crumbs_version = crumbs_version
        self.module = module
        self.caps = caps
        self.arms = arms
        self.staged = GET_VERSION
        self.commands: list[tuple[int, bytes]] = []
        self.armed = 0
        self.timeout_ms = 0
        self.tripped = 0
        self.trip_count = 0
        self.mode = 0
        self.flags = 0
        self.temperatures = [251, -32768]  # deci-degrees; -32768 means none
        self.setpoints = [0, 0]
        self.on_ms = [0, 0]
        self.periods = [1000, 1000]
        self.tc = [1, 2]
        # Faults, taken in order: "corrupt" makes the next read all 0xFF, an
        # OSError fails the next read or write with it.
        self.faults: list[str | OSError] = []
        self.fail_opcodes: dict[int, BaseException] = {}
        # Commands that the slice takes, but whose send then fails (an ack
        # lost to a clock stretch, say).
        self.fail_after: dict[int, BaseException] = {}
        # Replies to these opcodes read as all 0xFF while GET_STATE still works.
        self.corrupt_replies: set[int] = set()
        # GET_STATE replies served: the firmware feeds its watchdog on each.
        self.state_replies = 0
        self.watchdog_replies = 0

    def write(self, frame: bytes) -> None:
        message = decode(frame)
        if self.faults and isinstance(self.faults[0], OSError):
            fault = self.faults.pop(0)
            assert isinstance(fault, OSError)
            raise fault
        if message.opcode in self.fail_opcodes:
            raise self.fail_opcodes[message.opcode]
        if message.opcode == SET_REPLY:
            self.staged = message.data[0]
            return
        self.commands.append((message.opcode, bytes(message.data)))
        # As the firmware does (feastorg/Slice_RLHT#9): any command frame
        # clears a trip; SET_REPLY does not.
        self.tripped = 0
        op, data = message.opcode, message.data
        if op == SET_WATCHDOG and self.arms:
            self.timeout_ms = int.from_bytes(data[:2], "little")
            self.armed = 1 if self.timeout_ms else 0
        elif op == SET_MODE:
            self.mode = data[0]
        elif op == SET_SETPOINTS:
            self.setpoints = [
                int.from_bytes(data[i : i + 2], "little", signed=True) for i in (0, 2)
            ]
        elif op == SET_PERIODS:
            self.periods = [int.from_bytes(data[i : i + 2], "little") for i in (0, 2)]
        elif op == SET_TC:
            self.tc = [data[0], data[1]]
        if op in self.fail_after:
            raise self.fail_after[op]

    def trip(self) -> None:
        """The command watchdog expiring, as watchdogLogic() does it."""
        self.tripped = 1
        self.trip_count = (self.trip_count + 1) % 256
        self.setpoints = [0, 0]

    def reboot(self) -> None:
        """A power-cycle: the watchdog boots disarmed and its count at 0."""
        self.armed = 0
        self.timeout_ms = 0
        self.tripped = 0
        self.trip_count = 0
        self.setpoints = [0, 0]
        self.mode = 0
        self.tc = [1, 2]

    def reply(self) -> bytes:
        op = self.staged
        if op == GET_VERSION:
            return struct.pack("<HBBB", self.crumbs_version, *self.module)
        if op == GET_CAPS:
            return struct.pack("<BBI", 1, 1, self.caps)
        if op == GET_WATCHDOG:
            return struct.pack("<BHBB", self.armed, self.timeout_ms, self.tripped, self.trip_count)
        if op == GET_STATE:
            return struct.pack(
                "<BBhhhhHHHHB",
                self.mode,
                self.flags,
                *self.temperatures,
                *self.setpoints,
                *self.on_ms,
                *self.periods,
                self.tc[0] | self.tc[1] << 2,
            )
        return b""

    def read(self, count: int) -> bytes:
        if self.faults:
            fault = self.faults.pop(0)
            if isinstance(fault, OSError):
                raise fault
            return b"\xff" * count
        if self.staged == GET_STATE:
            self.state_replies += 1
        elif self.staged == GET_WATCHDOG:
            self.watchdog_replies += 1
        if self.staged in self.corrupt_replies:
            return b"\xff" * count
        frame = encode(Message(self.type_id, self.staged, self.reply()))
        return (frame + b"\xff" * count)[:count]


class FakeI2cBus:
    """A crumbs_i2c Bus holding fake slices by address."""

    def __init__(self, devices: dict[int, FakeRlht] | None = None):
        self.devices = devices or {}
        self.closed = False

    def _device(self, address: int) -> FakeRlht:
        if address not in self.devices:
            raise OSError(errno.ENXIO, "No such device or address")
        return self.devices[address]

    def write(self, address: int, data: bytes) -> None:
        self._device(address).write(data)

    def read(self, address: int, count: int) -> bytes:
        return self._device(address).read(count)

    def close(self) -> None:
        self.closed = True
