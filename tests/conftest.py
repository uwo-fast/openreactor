"""The web test harness shared across modules: the app over fake circuits."""

import time
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fakes import FakeActuator, FakeDcmt, FakeI2cBus, FakePort, FakeRlht
from fastapi.testclient import TestClient

from openreactor import cli
from openreactor.auth import hash_password
from openreactor.config import Config, DeviceConfig, parse_config
from openreactor.ezo import Value
from openreactor.service import running
from openreactor.web import create_app

PASSWORD = "correct horse"
HASH = hash_password(PASSWORD)
LOCAL = "http://127.0.0.1"

# The controller runs on its own thread in real time; run that time 20 times
# faster so the fake circuits' delays do not make the tests slow.
SPEED = 20.0


def fast_clock() -> float:
    return time.monotonic() * SPEED


def fast_sleep(seconds: float) -> None:
    time.sleep(seconds / SPEED)


DEVICES = """
[controller]
ezo_period_s = 0.5

[[device]]
name = "heater"
kind = "rlht"
bus = "/dev/i2c-1"
address = 0x0A
channels.jacket = { output = 1, tc = 1, max_setpoint = 80 }

[[device]]
name = "motors"
kind = "dcmt"
bus = "/dev/i2c-1"
address = 0x14
channels.stirrer = { motor = 1 }

[[device]]
name = "air"
kind = "ezo-hum"
bus = "/dev/i2c-1"
address = 0x6F
"""


def config_text(password: bool, database: Path) -> str:
    server = f'[server]\npassword_hash = "{HASH}"\n' if password else ""
    return f'{server}[storage]\ndatabase = "{database}"\n{DEVICES}'


class Bench:
    """The app over fake circuits, with what the tests look at."""

    def __init__(self, tmp_path: Path, password: bool):
        self.database = tmp_path / "state" / "openreactor.db"
        self.text = config_text(password, self.database)
        self.config: Config = parse_config(tomllib.loads(self.text))
        self.port = FakePort(
            "hum",
            (Value("humidity", 41.0, "%"), Value("temperature", 22.5, "°C")),
            wait_ms=300,
        )
        self.log: list[str] = []
        # The config's RLHT, answering on a fake bus.
        self.rlht = FakeRlht()
        self.rlht_address = 0x0A  # where it answers; the config says 0x0A
        self.dcmt = FakeDcmt()
        # What serve would print: start-up problems and events, then events.
        self.printed: list = []
        self.lock = tmp_path / "openreactor.lock"

        def open_port(d: DeviceConfig) -> FakePort:
            return self.port

        def start():
            return running(
                self.config,
                self.text,
                open_port=open_port,
                actuators=[FakeActuator("jacket", self.log)],
                open_bus=lambda path: FakeI2cBus({self.rlht_address: self.rlht, 0x14: self.dcmt}),
                lock_path=self.lock,
                clock=fast_clock,
                sleep=fast_sleep,
                on_event=self.printed.append,
            )

        self.app = create_app(self.config, start)


@pytest.fixture
def open_bench(tmp_path: Path) -> Iterator[tuple[Bench, TestClient]]:
    bench = Bench(tmp_path, password=False)
    with TestClient(bench.app, base_url=LOCAL) as client:
        yield bench, client


@pytest.fixture
def locked_bench(tmp_path: Path) -> Iterator[tuple[Bench, TestClient]]:
    bench = Bench(tmp_path, password=True)
    with TestClient(bench.app, base_url=LOCAL) as client:
        yield bench, client


def until(check: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


@pytest.fixture
def lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The machine-wide lock lives in /run/lock; tests use their own file.
    Not autouse: a module that needs it everywhere marks itself with
    ``pytestmark = pytest.mark.usefixtures("lock")``."""
    path = tmp_path / "openreactor.lock"
    monkeypatch.setattr(cli, "lock_path", path)
    return path
