import io
import urllib.error
from pathlib import Path

import pytest
from fakes import FakeI2cBus, FakeRlht
from fastapi.testclient import TestClient
from test_web import LOCAL, Bench, until

from openreactor import cli
from openreactor.auth import hash_password
from openreactor.lock import ControllerLock

CONFIG = """
[server]
host = "{host}"
port = 8123
{password}
[[device]]
name = "heater"
kind = "rlht"
bus = "/dev/i2c-1"
address = 0x0A
channels.jacket = {{ output = 1, tc = 1 }}
"""


@pytest.fixture(autouse=True)
def lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "openreactor.lock"
    monkeypatch.setattr(cli, "lock_path", path)
    return path


@pytest.fixture
def rlht(monkeypatch: pytest.MonkeyPatch) -> FakeRlht:
    slice_ = FakeRlht()
    monkeypatch.setattr(cli, "open_bus", lambda path: FakeI2cBus({0x0A: slice_}))
    return slice_


def write_config(tmp_path: Path, host: str = "127.0.0.1", password: bool = False) -> str:
    line = f'password_hash = "{hash_password("bench")}"' if password else ""
    path = tmp_path / "openreactor.toml"
    path.write_text(CONFIG.format(host=host, password=line))
    return str(path)


def test_with_nothing_running_it_reads_the_slice_and_sends_it_nothing(
    tmp_path: Path, rlht: FakeRlht, capsys: pytest.CaptureFixture[str]
):
    rlht.armed, rlht.timeout_ms, rlht.trip_count = 1, 5000, 2
    rlht.temperatures = [376, -32768]
    rlht.setpoints = [370, 0]
    rlht.on_ms = [250, 0]
    assert cli.main(["status", "-c", write_config(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "heater  0x0A  RLHT 1.0.0, CRUMBS 0.15.0" in out
    assert "watchdog  armed 5000 ms, trips 2" in out
    assert "e-stop    clear" in out and "mode      closed loop" in out
    assert "jacket      37.6 °C  setpoint   37.0  duty  25 %  tc 1" in out
    assert "output 2    no reading" in out
    assert "(read from the bus: no controller is running)" in out
    # Only replies were asked for: no command reached the slice.
    assert rlht.commands == []


def test_a_tripped_or_e_stopped_slice_says_so(
    tmp_path: Path, rlht: FakeRlht, capsys: pytest.CaptureFixture[str]
):
    rlht.armed, rlht.timeout_ms, rlht.tripped, rlht.trip_count = 1, 5000, 1, 1
    rlht.flags = 0x01
    assert cli.main(["status", "-c", write_config(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "state     e-stop held on the slice" in out
    assert "trips 1, TRIPPED" in out and "e-stop    held" in out


def test_a_slice_that_does_not_answer_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(cli, "open_bus", lambda path: FakeI2cBus({}))
    assert cli.main(["status", "-c", write_config(tmp_path)]) == 0
    assert "state     error: no answer on the bus" in capsys.readouterr().out


def test_while_a_server_runs_it_asks_the_server(
    tmp_path: Path,
    lock: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    bench = Bench(tmp_path, password=False)
    asked: list[tuple[str, str | None]] = []
    with TestClient(bench.app, base_url=LOCAL) as client:
        client.put("/api/v1/channels/jacket/setpoint", json={"value": 37})
        until(lambda: client.get("/api/v1/status").json()["slices"][0]["desired_c"][0] == 37.0)
        # The slice has not taken it yet: status shows what is wanted.
        bench.rlht.setpoints = [0, 0]
        until(
            lambda: (
                client.get("/api/v1/status").json()["slices"][0]["outputs"][0]["setpoint_c"] == 0.0
            )
        )

        def fetch(url: str, password: str | None) -> dict:
            asked.append((url, password))
            return client.get("/api/v1/status").json()

        monkeypatch.setattr(cli, "fetch_status", fetch)
        # The bench's server holds the lock, as serve does.
        assert cli.main(["status", "-c", write_config(tmp_path, host="0.0.0.0")]) == 0
    out = capsys.readouterr().out
    # A server bound to every address is asked on loopback, without a password.
    assert asked == [("http://127.0.0.1:8123/api/v1/status", None)]
    assert "watchdog  armed 5000 ms, trips 0" in out
    assert "setpoint    0.0" in out and "(wanted 37.0)" in out
    assert "air        ezo-hum  ok" in out
    assert "(from the server at http://127.0.0.1:8123/api/v1/status)" in out


def test_the_server_password_comes_from_the_environment_never_argv(
    tmp_path: Path, lock: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    asked: list[str | None] = []
    monkeypatch.setattr(cli, "fetch_status", lambda url, pw: asked.append(pw) or {"slices": []})
    monkeypatch.setattr("sys.stdin", io.StringIO())  # not a terminal: no prompt
    config = write_config(tmp_path, password=True)
    with ControllerLock(lock):
        monkeypatch.delenv("OPENREACTOR_PASSWORD", raising=False)
        assert cli.main(["status", "-c", config]) == 1
        assert "set OPENREACTOR_PASSWORD" in capsys.readouterr().err
        monkeypatch.setenv("OPENREACTOR_PASSWORD", "bench")
        assert cli.main(["status", "-c", config]) == 0
    assert asked == ["bench"]


def test_a_refused_password_or_no_server_is_an_error(
    tmp_path: Path, lock: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    def refused(url: str, password: str | None) -> dict:
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)  # type: ignore[arg-type]

    def nobody(url: str, password: str | None) -> dict:
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    config = write_config(tmp_path)
    with ControllerLock(lock):
        monkeypatch.setattr(cli, "fetch_status", refused)
        assert cli.main(["status", "-c", config]) == 1
        assert "the password was refused" in capsys.readouterr().err
        monkeypatch.setattr(cli, "fetch_status", nobody)
        assert cli.main(["status", "-c", config, "--url", "http://pi:9000/api/v1/status"]) == 1
    err = capsys.readouterr().err
    assert "holds the bus, and no server answered at http://pi:9000/api/v1/status" in err
    assert "pass --url" in err


def test_fetch_status_sends_the_password_as_a_bearer_token():
    import http.server
    import json
    import threading

    seen: dict[str, str | None] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen["path"] = self.path
            seen["auth"] = self.headers.get("Authorization")
            body = json.dumps({"slices": [], "devices": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/v1/status"
        assert cli.fetch_status(url, "bench") == {"slices": [], "devices": []}
    finally:
        server.shutdown()
    assert seen == {"path": "/api/v1/status", "auth": "Bearer bench"}
