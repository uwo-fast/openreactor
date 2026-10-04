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
        self.staged = 0x00
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
        # GET_STATE replies served: the firmware feeds its watchdog on each.
        self.state_replies = 0
        self.watchdog_replies = 0

    def write(self, frame: bytes) -> None:
        from crumbs_i2c import decode

        message = decode(frame)
        if self.faults and isinstance(self.faults[0], OSError):
            fault = self.faults.pop(0)
            assert isinstance(fault, OSError)
            raise fault
        if message.opcode in self.fail_opcodes:
            raise self.fail_opcodes[message.opcode]
        if message.opcode == 0xFE:
            self.staged = message.data[0]
            return
        self.commands.append((message.opcode, bytes(message.data)))
        op, data = message.opcode, message.data
        if op == 0x7E and self.arms:  # SET_WATCHDOG
            self.timeout_ms = int.from_bytes(data[:2], "little")
            self.armed = 1 if self.timeout_ms else 0
        elif op == 0x01:
            self.mode = data[0]
        elif op == 0x02:
            self.setpoints = [
                int.from_bytes(data[i : i + 2], "little", signed=True) for i in (0, 2)
            ]
        elif op == 0x04:
            self.periods = [int.from_bytes(data[i : i + 2], "little") for i in (0, 2)]
        elif op == 0x05:
            self.tc = [data[0], data[1]]

    def reply(self) -> bytes:
        import struct

        op = self.staged
        if op == 0x00:
            return struct.pack("<HBBB", self.crumbs_version, *self.module)
        if op == 0x7F:
            return struct.pack("<BBI", 1, 1, self.caps)
        if op == 0x7D:
            return struct.pack("<BHBB", self.armed, self.timeout_ms, self.tripped, self.trip_count)
        if op == 0x80:
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
        from crumbs_i2c import Message, encode

        if self.faults:
            fault = self.faults.pop(0)
            if isinstance(fault, OSError):
                raise fault
            return b"\xff" * count
        if self.staged == 0x80:
            self.state_replies += 1
        elif self.staged == 0x7D:
            self.watchdog_replies += 1
        frame = encode(Message(self.type_id, self.staged, self.reply()))
        return (frame + b"\xff" * count)[:count]


class FakeI2cBus:
    """A crumbs_i2c Bus holding fake slices by address."""

    def __init__(self, devices: dict[int, FakeRlht] | None = None):
        self.devices = devices or {}
        self.closed = False

    def _device(self, address: int) -> FakeRlht:
        import errno

        if address not in self.devices:
            raise OSError(errno.ENXIO, "No such device or address")
        return self.devices[address]

    def write(self, address: int, data: bytes) -> None:
        self._device(address).write(data)

    def read(self, address: int, count: int) -> bytes:
        return self._device(address).read(count)

    def close(self) -> None:
        self.closed = True
