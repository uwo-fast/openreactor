import io
import json
import urllib.error
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_web import LOCAL, Bench, until

from openreactor import cli
from openreactor.lock import ControllerLock

CONFIG = """
[server]
host = "{host}"
port = 8123

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


@pytest.fixture(autouse=True)
def no_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    # status never reads a slice itself: any reply feeds its watchdog.
    def refuse(path: str):
        raise AssertionError(f"status opened {path}")

    monkeypatch.setattr(cli, "open_bus", refuse)


@pytest.fixture(autouse=True)
def no_env_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENREACTOR_PASSWORD", raising=False)


def write_config(tmp_path: Path, host: str = "127.0.0.1") -> str:
    path = tmp_path / "openreactor.toml"
    path.write_text(CONFIG.format(host=host))
    return str(path)


def refused(url: str, password: str | None) -> dict:
    raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))


def test_with_a_server_running_it_shows_what_the_server_sees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    bench = Bench(tmp_path, password=False)
    asked: list[tuple[str, str | None]] = []
    with TestClient(bench.app, base_url=LOCAL) as client:
        client.put("/api/v1/channels/jacket/setpoint", json={"value": 37})
        until(lambda: client.get("/api/v1/status").json()["slices"][0]["desired_c"][0] == 37.0)
        # The slice has not taken it: status shows what is wanted.
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
        assert cli.main(["status", "-c", write_config(tmp_path, host="0.0.0.0")]) == 0
    out = capsys.readouterr().out
    # A server bound to every address is asked on loopback.
    assert asked == [("http://127.0.0.1:8123/api/v1/status", None)]
    assert "heater  0x0A  RLHT 1.0.0, CRUMBS 0.15.0" in out
    assert "watchdog  armed 5000 ms, trips 0" in out
    assert "e-stop    clear" in out and "mode      closed loop" in out
    assert "jacket      25.1 °C  setpoint    0.0  duty   0 %  tc 1  (wanted 37.0)" in out
    assert "output 2    no reading" in out
    assert "air        ezo-hum  ok" in out
    assert out.rstrip().endswith("(from the server at http://127.0.0.1:8123/api/v1/status)")


def test_with_nothing_running_it_reads_nothing_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(cli, "fetch_status", refused)
    assert cli.main(["status", "-c", write_config(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "no server answered at http://127.0.0.1:8123/api/v1/status" in err
    assert "Nothing holds the bus either: start openreactor serve" in err


def test_with_another_controller_on_the_bus_it_names_it(
    tmp_path: Path, lock: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(cli, "fetch_status", refused)
    with ControllerLock(lock):
        assert cli.main(["status", "-c", write_config(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "holds the bus; for a server on another address, pass --url" in err


def test_url_and_the_environment_password_are_used_whatever_the_local_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    asked: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        cli, "fetch_status", lambda url, pw: asked.append((url, pw)) or {"slices": []}
    )
    monkeypatch.setenv("OPENREACTOR_PASSWORD", "bench")
    url = "http://pi:9000/api/v1/status"
    assert cli.main(["status", "-c", write_config(tmp_path), "--url", url]) == 0
    assert asked == [(url, "bench")]


def test_a_server_that_wants_a_password_gets_one_from_a_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    asked: list[str | None] = []

    def fetch(url: str, password: str | None) -> dict:
        asked.append(password)
        if password != "bench":
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)  # type: ignore[arg-type]
        return {"slices": []}

    monkeypatch.setattr(cli, "fetch_status", fetch)
    config = write_config(tmp_path)

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", Terminal())
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "bench")
    assert cli.main(["status", "-c", config]) == 0
    assert asked == [None, "bench"]
    # Without a terminal there is no prompt: it says what to set.
    monkeypatch.setattr("sys.stdin", io.StringIO())
    assert cli.main(["status", "-c", config]) == 1
    assert "needs a password: set OPENREACTOR_PASSWORD" in capsys.readouterr().err
    # A password that was sent and refused is reported as refused.
    monkeypatch.setenv("OPENREACTOR_PASSWORD", "wrong")
    assert cli.main(["status", "-c", config]) == 1
    assert "the password was refused" in capsys.readouterr().err


def test_a_slow_server_is_reported_as_slow_not_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    def slow(url: str, password: str | None) -> dict:
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(cli, "fetch_status", slow)
    assert cli.main(["status", "-c", write_config(tmp_path)]) == 1
    assert "did not answer within 5 s" in capsys.readouterr().err


def test_an_older_server_or_another_service_is_reported_plainly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    config = write_config(tmp_path)
    older = {"devices": [{"name": "heater", "kind": "rlht", "status": "unreachable: 3 polls"}]}
    monkeypatch.setattr(cli, "fetch_status", lambda url, pw: older)
    assert cli.main(["status", "-c", config]) == 0
    out = capsys.readouterr().out
    assert "predates slice detail" in out and "heater     rlht     unreachable: 3 polls" in out
    for answer in ([1, 2], {"slices": [{"name": "heater"}]}):
        monkeypatch.setattr(cli, "fetch_status", lambda url, pw, a=answer: a)
        assert cli.main(["status", "-c", config]) == 1
        assert "did not answer with an openreactor status" in capsys.readouterr().err

    def not_json(url: str, password: str | None) -> dict:
        raise json.JSONDecodeError("Expecting value", "<html>", 0)

    monkeypatch.setattr(cli, "fetch_status", not_json)
    assert cli.main(["status", "-c", config]) == 1
    assert "did not answer with JSON" in capsys.readouterr().err


def test_fetch_status_sends_the_password_as_a_bearer_token():
    import http.server
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


def test_a_tripped_e_stopped_or_unprotected_slice_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    output = {"channel": "jacket", "tc": 1, "temperature_c": 30.0, "setpoint_c": 0.0}
    output |= {"output": 1, "on_ms": 0, "period_ms": 0}
    tripped = {"armed": True, "timeout_ms": 5000, "tripped": True, "trip_count": 3}
    status = {
        "slices": [
            {"name": "heater", "address": 10, "state": "e-stop held on the slice",
             "version": "RLHT 1.0.0, CRUMBS 0.15.0", "caps": 127, "mode": "closed loop",
             "estop": True, "outputs": [output], "watchdog": tripped, "desired_c": [0.0, 0.0]},
            {"name": "lid", "address": 11, "state": "read-only: the slice has no command watchdog",
             "version": "RLHT 1.0.0, CRUMBS 0.15.0", "caps": 63, "mode": "closed loop",
             "estop": False, "outputs": [], "watchdog": None, "desired_c": [0.0, 0.0]},
        ],
        "devices": [],
    }  # fmt: skip
    monkeypatch.setattr(cli, "fetch_status", lambda url, pw: status)
    assert cli.main(["status", "-c", write_config(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "state     e-stop held on the slice" in out
    assert "watchdog  armed 5000 ms, trips 3, TRIPPED" in out and "e-stop    held" in out
    assert "duty   0 %" in out  # a period of 0 is no duty, not a division by zero
    assert "state     read-only: the slice has no command watchdog" in out
    assert "watchdog  none: the slice has no command watchdog" in out
