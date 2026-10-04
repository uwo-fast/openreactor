import io
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fakes import FakeActuator, FakePort
from fastapi.testclient import TestClient

from openreactor import cli
from openreactor.auth import hash_password
from openreactor.config import Config, DeviceConfig, parse_config
from openreactor.ezo import Value
from openreactor.lock import ControllerLock
from openreactor.service import running
from openreactor.storage import Store
from openreactor.web import MAX_BODY_BYTES, SESSION_COOKIE, create_app

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
channels.jacket = { output = 1, tc = 1 }

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
        import tomllib

        self.config: Config = parse_config(tomllib.loads(self.text))
        self.port = FakePort(
            "hum",
            (Value("humidity", 41.0, "%"), Value("temperature", 22.5, "°C")),
            wait_ms=300,
        )
        self.log: list[str] = []
        self.lock = tmp_path / "openreactor.lock"

        def open_port(d: DeviceConfig) -> FakePort:
            return self.port

        def start():
            return running(
                self.config,
                self.text,
                open_port=open_port,
                actuators=[FakeActuator("jacket", self.log)],
                lock_path=self.lock,
                clock=fast_clock,
                sleep=fast_sleep,
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


def sign_in(client: TestClient) -> None:
    r = client.post("/login", data={"password": PASSWORD}, headers={"Origin": LOCAL})
    assert r.status_code == 204
    assert SESSION_COOKIE in client.cookies


def until(check: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


BEARER = {"Authorization": f"Bearer {PASSWORD}"}


# Without a password: loopback only


def test_status_lists_the_devices(open_bench):
    _, client = open_bench
    r = client.get("/api/v1/status")
    assert r.status_code == 200
    body = r.json()
    assert body["run"] is None
    assert body["devices"] == [
        {"name": "heater", "kind": "rlht", "status": "slices are not supported yet"},
        {"name": "air", "kind": "ezo-hum", "status": "ok"},
    ]


def test_a_foreign_host_name_is_refused_without_a_password(open_bench):
    """DNS rebinding: a page on evil.example resolved to 127.0.0.1 reaches
    the server, but under its own Host name."""
    _, client = open_bench
    assert client.get("/api/v1/status", headers={"Host": "evil.example:8080"}).status_code == 403
    for host in ("[::1", "", "127.0.0.1.evil.example"):
        assert client.get("/api/v1/status", headers={"Host": host}).status_code == 403, host
    for host in ("localhost:8080", "127.0.0.1", "[::1]:8080"):
        assert client.get("/api/v1/status", headers={"Host": host}).status_code == 200, host


def test_a_cross_site_post_is_refused(open_bench):
    bench, client = open_bench
    r = client.post("/api/v1/stop-all", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert bench.log == []
    assert client.post("/api/v1/stop-all", headers={"Origin": LOCAL}).status_code == 200
    # A script sends no Origin at all.
    assert client.post("/api/v1/stop-all").status_code == 200
    assert bench.log == ["safe jacket", "safe jacket"]


def test_channels_show_the_latest_reading(open_bench):
    _, client = open_bench
    until(lambda: len(client.get("/api/v1/channels").json()) == 2)
    channels = {c["name"]: c for c in client.get("/api/v1/channels").json()}
    assert channels["air.humidity"]["value"] == 41.0
    assert channels["air.humidity"]["unit"] == "%"
    assert channels["air.temperature"]["outcome"] == "ok"


def test_stop_all_returns_its_events(open_bench):
    bench, client = open_bench
    events = client.post("/api/v1/stop-all").json()
    assert [(e["kind"], e["device"], e["result"]) for e in events] == [("stop-all", "jacket", "ok")]
    assert bench.log == ["safe jacket"]


def test_a_setpoint_on_a_slice_is_not_supported_yet(open_bench):
    _, client = open_bench
    r = client.put("/api/v1/channels/jacket/setpoint", json={"value": 37.0})
    assert r.status_code == 409
    assert "slices are not supported yet" in r.json()["detail"]
    r = client.put("/api/v1/channels/nothing/setpoint", json={"value": 37.0})
    assert r.status_code == 404
    r = client.put("/api/v1/channels/jacket/setpoint", json={"value": "hot"})
    assert r.status_code == 422


def test_a_run_starts_records_stops_and_exports(open_bench):
    bench, client = open_bench
    r = client.post("/api/v1/runs", json={"name": "brew", "notes": "batch 4"})
    assert r.status_code == 201
    run = r.json()["id"]
    assert client.post("/api/v1/runs", json={"name": "again"}).status_code == 409
    assert client.get("/api/v1/status").json()["run"] == run
    until(lambda: _readings(bench.database) >= 2)

    r = client.post("/api/v1/runs/current/stop")
    assert r.json() == {"id": run, "status": "stopped"}
    assert client.post("/api/v1/runs/current/stop").status_code == 409
    [listed] = client.get("/api/v1/runs").json()
    assert (listed["name"], listed["notes"], listed["status"]) == ("brew", "batch 4", "stopped")

    r = client.get(f"/api/v1/runs/{run}/export")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert f'filename="run-{run}.zip"' in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert set(z.namelist()) == {"readings.csv", "events.csv", "run.json"}
        assert "air.humidity,41.0,%" in z.read("readings.csv").decode()
    assert client.get("/api/v1/runs/99/export").status_code == 404


def test_a_run_needs_a_name(open_bench):
    _, client = open_bench
    assert client.post("/api/v1/runs", json={"name": ""}).status_code == 422


def test_no_runs_yet_is_an_empty_list(open_bench):
    bench, client = open_bench
    assert client.get("/api/v1/runs").json() == []
    assert client.get("/api/v1/runs/1/export").status_code == 404
    assert not bench.database.exists()


def _readings(database: Path) -> int:
    if not database.exists():
        return 0
    store = Store(database, read_only=True)
    try:
        return sum(1 for _ in store._db.execute("SELECT 1 FROM readings"))  # pyright: ignore[reportPrivateUsage]
    finally:
        store.close()


def test_ezo_calibration_shows_sets_and_clears(open_bench):
    bench, client = open_bench
    r = client.get("/api/v1/ezo/air/calibration")
    assert r.json() == {"device": "air", "status": "not calibrated"}
    r = client.post("/api/v1/ezo/air/calibration", json={"point": "temperature", "value": 25.0})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "calibrated at temperature"
    assert "cal temperature 25.0" in bench.port.sent
    r = client.delete("/api/v1/ezo/air/calibration")
    assert r.json()["status"] == "not calibrated"


def test_a_bad_calibration_is_refused_before_anything_is_sent(open_bench):
    bench, client = open_bench
    r = client.post("/api/v1/ezo/air/calibration", json={"point": "mid", "value": 7.0})
    assert r.status_code == 422
    assert "no calibration point 'mid'" in r.json()["detail"]
    assert client.get("/api/v1/ezo/nothing/calibration").status_code == 404
    assert client.get("/api/v1/ezo/heater/calibration").status_code == 404
    assert not any(s.startswith("cal ") for s in bench.port.sent)


def test_shutdown_sends_stop_all_and_ends_the_run(tmp_path: Path):
    bench = Bench(tmp_path, password=False)
    with TestClient(bench.app, base_url=LOCAL) as client:
        run = client.post("/api/v1/runs", json={"name": "brew"}).json()["id"]
    assert bench.log == ["safe jacket"]
    assert bench.port.closed
    store = Store(bench.database, read_only=True)
    try:
        assert store.run(run).status == "stopped"  # type: ignore[union-attr]
    finally:
        store.close()


def test_the_api_docs_are_behind_authorization(locked_bench):
    _, client = locked_bench
    for public in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(public).status_code == 404, public
    assert client.get("/api/v1/openapi.json").status_code == 401
    r = client.get("/api/v1/openapi.json", headers=BEARER)
    assert r.status_code == 200
    assert "/api/v1/runs" in r.json()["paths"]


# With a password


def test_without_credentials_every_route_is_refused_without_a_basic_challenge(locked_bench):
    bench, client = locked_bench
    for method, path in [
        ("GET", "/api/v1/status"),
        ("GET", "/api/v1/channels"),
        ("PUT", "/api/v1/channels/jacket/setpoint"),
        ("POST", "/api/v1/stop-all"),
        ("GET", "/api/v1/runs"),
        ("POST", "/api/v1/runs"),
        ("POST", "/api/v1/runs/current/stop"),
        ("GET", "/api/v1/runs/1/export"),
        ("GET", "/api/v1/ezo/air/calibration"),
        ("POST", "/api/v1/ezo/air/calibration"),
        ("DELETE", "/api/v1/ezo/air/calibration"),
    ]:
        r = client.request(method, path, json={"value": 1.0, "name": "x", "point": "dry"})
        assert r.status_code == 401, (method, path)
        assert "basic" not in r.headers.get("www-authenticate", "").lower()
    assert bench.log == []


def test_bearer_password_lets_a_script_in(locked_bench):
    bench, client = locked_bench
    assert client.get("/api/v1/status", headers=BEARER).status_code == 200
    wrong = {"Authorization": "Bearer wrong"}
    assert client.get("/api/v1/status", headers=wrong).status_code == 401
    for scheme in ("Basic", "Token", ""):
        header = {"Authorization": f"{scheme} {PASSWORD}".strip()}
        assert client.get("/api/v1/status", headers=header).status_code == 401, scheme
    assert client.post("/api/v1/stop-all", headers=BEARER).status_code == 200
    assert bench.log == ["safe jacket"]


def test_a_foreign_host_is_fine_with_a_password(locked_bench):
    _, client = locked_bench
    r = client.get("/api/v1/status", headers={**BEARER, "Host": "reactor.lab:8080"})
    assert r.status_code == 200


def test_a_browser_signs_in_and_out(locked_bench):
    _, client = locked_bench
    r = client.post("/login", data={"password": "wrong"}, headers={"Origin": LOCAL})
    assert r.status_code == 401
    sign_in(client)
    assert client.get("/api/v1/status").status_code == 200
    assert client.post("/logout", headers={"Origin": LOCAL}).status_code == 204
    assert client.get("/api/v1/status").status_code == 401


def test_sign_in_takes_json_too(locked_bench):
    _, client = locked_bench
    r = client.post("/login", json={"password": PASSWORD})
    assert r.status_code == 204
    assert client.get("/api/v1/status").status_code == 200


def test_a_signed_in_change_needs_a_matching_origin(locked_bench):
    bench, client = locked_bench
    sign_in(client)
    assert client.post("/api/v1/stop-all").status_code == 403
    assert (
        client.post("/api/v1/stop-all", headers={"Origin": "http://evil.example"}).status_code
        == 403
    )
    assert bench.log == []
    assert client.post("/api/v1/stop-all", headers={"Origin": LOCAL}).status_code == 200
    assert bench.log == ["safe jacket"]


def test_sign_in_from_another_site_is_refused(locked_bench):
    _, client = locked_bench
    r = client.post(
        "/login", data={"password": PASSWORD}, headers={"Origin": "http://evil.example"}
    )
    assert r.status_code == 403
    assert SESSION_COOKIE not in client.cookies


def test_a_forged_session_cookie_is_refused(locked_bench):
    _, client = locked_bench
    client.cookies.set(SESSION_COOKIE, "eyJzaWduZWRfaW4iOiB0cnVlfQ==.forged.sig")
    assert client.get("/api/v1/status").status_code == 401


# openreactor serve and hash-password


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(cli, "run_server", lambda app, host, port: calls.append((host, port)))
    monkeypatch.setattr(cli, "lock_path", tmp_path / "openreactor.lock")
    return calls


def write_config(tmp_path: Path, password: bool) -> str:
    path = tmp_path / "openreactor.toml"
    path.write_text(config_text(password, tmp_path / "state" / "openreactor.db"))
    return str(path)


def test_serve_refuses_a_network_address_without_a_password(tmp_path: Path, served, capsys):
    config = write_config(tmp_path, password=False)
    for host in ("0.0.0.0", "192.168.1.20", "::", "reactor.lab"):
        assert cli.main(["serve", "-c", config, "--host", host]) == 1, host
        assert "refusing to serve on" in capsys.readouterr().err
    assert served == []
    for host in ("127.0.0.1", "localhost", "::1", "127.0.0.2"):
        assert cli.main(["serve", "-c", config, "--host", host]) == 0, host
    assert [h for h, _ in served] == ["127.0.0.1", "localhost", "::1", "127.0.0.2"]


def test_serve_binds_a_network_address_with_a_password(tmp_path: Path, served):
    config = write_config(tmp_path, password=True)
    assert cli.main(["serve", "-c", config, "--host", "0.0.0.0", "--port", "9000"]) == 0
    assert served == [("0.0.0.0", 9000)]


def test_serve_defaults_to_loopback(tmp_path: Path, served):
    config = write_config(tmp_path, password=False)
    assert cli.main(["serve", "-c", config]) == 0
    assert served == [("127.0.0.1", 8080)]


def test_serve_refuses_while_another_controller_runs(tmp_path: Path, served, capsys):
    config = write_config(tmp_path, password=False)
    with ControllerLock(tmp_path / "openreactor.lock"):
        assert cli.main(["serve", "-c", config]) == 1
    assert "another controller holds" in capsys.readouterr().err
    assert served == []


def test_a_malformed_password_hash_fails_the_config(tmp_path: Path, capsys):
    path = tmp_path / "openreactor.toml"
    path.write_text('[server]\npassword_hash = "hunter2"\n')
    assert cli.main(["check-config", str(path)]) == 1
    assert "server.password_hash: is not a hash" in capsys.readouterr().err


def test_hash_password_reads_stdin(monkeypatch: pytest.MonkeyPatch, capsys):
    from openreactor.auth import verify_password

    monkeypatch.setattr("sys.stdin", io.StringIO("s3cret\n"))
    assert cli.main(["hash-password"]) == 0
    made = capsys.readouterr().out.strip()
    assert verify_password("s3cret", made)
    assert not verify_password("s3cret\n", made)
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert cli.main(["hash-password"]) == 1


def test_serve_reports_an_unusable_lock_path(tmp_path: Path, served, monkeypatch, capsys):
    config = write_config(tmp_path, password=False)
    monkeypatch.setattr(cli, "lock_path", tmp_path / "missing" / "openreactor.lock")
    assert cli.main(["serve", "-c", config]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "missing/openreactor.lock" in err
    assert served == []


def test_calibrating_a_device_that_failed_at_startup_says_why(tmp_path: Path):
    from openreactor.ezo import EzoDeviceError

    bench = Bench(tmp_path, password=False)
    bench.port.family_error = EzoDeviceError("cannot open /dev/i2c-1 at 0x6F: no reply")
    with TestClient(bench.app, base_url=LOCAL) as client:
        r = client.get("/api/v1/ezo/air/calibration")
        assert r.status_code == 409
        assert r.json()["detail"].startswith("air is out of use since startup")
        status = client.get("/api/v1/status").json()
        assert status["devices"][1]["status"] != "ok"


# Abuse from the network: none of it may delay or block stop-all


def test_a_large_body_is_refused_before_it_is_read(locked_bench):
    _, client = locked_bench
    big = "x" * (MAX_BODY_BYTES + 1)
    r = client.post("/login", data={"password": big}, headers={"Origin": LOCAL})
    assert r.status_code == 413
    r = client.post("/api/v1/runs", json={"name": big})
    assert r.status_code == 413
    # Without a Content-Length the bytes are counted as they arrive.
    chunks = (b"x" * 4096 for _ in range(MAX_BODY_BYTES // 4096 + 2))
    r = client.post("/login", content=chunks, headers={"Origin": LOCAL})
    assert r.status_code == 413
    assert client.post("/login", json={"password": PASSWORD}).status_code == 204


def test_a_guess_during_another_check_is_refused_not_queued(monkeypatch):
    import threading

    from openreactor import web
    from openreactor.web import Busy, Gate

    gate = Gate(HASH)
    assert gate.check(PASSWORD)
    entered, release = threading.Event(), threading.Event()

    def slow_verify(password: str, stored: str) -> bool:
        entered.set()
        release.wait(5)
        return False

    monkeypatch.setattr(web, "verify_password", slow_verify)
    guess = threading.Thread(target=lambda: gate.check("wrong"))
    guess.start()
    assert entered.wait(5)
    with pytest.raises(Busy):
        gate.check("also wrong")
    # The known password is still let in at once.
    assert gate.check(PASSWORD)
    release.set()
    guess.join()


def test_a_busy_gate_answers_429(locked_bench, monkeypatch):
    from openreactor.web import Busy

    bench, client = locked_bench

    def busy(password: str) -> bool:
        raise Busy

    monkeypatch.setattr(bench.app.state.gate, "check", busy)
    assert client.get("/api/v1/status", headers={"Authorization": "Bearer nope"}).status_code == 429
    r = client.post("/login", json={"password": "nope"})
    assert r.status_code == 429


def test_signing_out_revokes_a_copied_cookie(locked_bench):
    _, client = locked_bench
    sign_in(client)
    copied = client.cookies[SESSION_COOKIE]
    assert client.post("/logout", headers={"Origin": LOCAL}).status_code == 204
    client.cookies.set(SESSION_COOKIE, copied)
    assert client.get("/api/v1/status").status_code == 401


def test_put_and_delete_need_a_matching_origin_too(locked_bench):
    bench, client = locked_bench
    sign_in(client)
    r = client.put("/api/v1/channels/jacket/setpoint", json={"value": 1.0})
    assert r.status_code == 403
    assert client.delete("/api/v1/ezo/air/calibration").status_code == 403
    assert "cal clear" not in bench.port.sent


def test_stop_now_sends_stop_all_without_waiting(open_bench):
    bench, client = open_bench
    stop_now = bench.app.state.stop_now
    assert stop_now is not None
    stop_now()
    until(lambda: bench.log == ["safe jacket"])


# Errors


def test_a_circuit_that_refuses_a_calibration_is_a_502(open_bench):
    from openreactor.ezo import Outcome

    bench, client = open_bench
    bench.port.ack = Outcome.FAIL
    r = client.post("/api/v1/ezo/air/calibration", json={"point": "temperature", "value": 25.0})
    assert r.status_code == 502
    assert r.json()["detail"] == f"the circuit answered {Outcome.FAIL.value}"


def test_an_impossible_run_number_is_a_422(open_bench):
    _, client = open_bench
    for run in ("0", "99999999999999999999"):
        assert client.get(f"/api/v1/runs/{run}/export").status_code == 422, run


def test_an_unwritable_database_is_a_503(tmp_path: Path):
    bench = Bench(tmp_path, password=False)
    bench.database.parent.mkdir()
    bench.database.parent.chmod(0o500)
    try:
        with TestClient(bench.app, base_url=LOCAL) as client:
            r = client.post("/api/v1/runs", json={"name": "brew"})
            assert r.status_code == 503, r.text
            assert client.post("/api/v1/stop-all").status_code == 200
    finally:
        bench.database.parent.chmod(0o700)


def test_status_shows_that_recording_stopped(open_bench, monkeypatch):
    import sqlite3

    bench, client = open_bench

    def full(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(Store, "add_readings", full)
    run = client.post("/api/v1/runs", json={"name": "brew"}).json()["id"]
    until(lambda: client.get("/api/v1/status").json()["recording_failed"] is not None)
    status = client.get("/api/v1/status").json()
    assert status["run"] == run
    assert status["recording_failed"] == "database or disk is full"
    r = client.post("/api/v1/runs", json={"name": "again"})
    assert r.status_code == 409 and "stopped recording" in r.json()["detail"]
    assert client.post("/api/v1/runs/current/stop").json() == {"id": run, "status": "interrupted"}


def test_an_action_the_controller_never_reached_is_cancelled(tmp_path: Path, monkeypatch):
    from openreactor import service
    from openreactor.controller import Controller
    from openreactor.ezo import EzoReader
    from openreactor.service import Service

    monkeypatch.setattr(service, "ACTION_TIMEOUT_S", 0.05)
    bench = Bench(tmp_path, password=False)
    controller = Controller(EzoReader([]), [], ezo_period_s=2.0, auto_read=True)  # not ticking
    svc = Service(bench.config, bench.text, controller, bench.database)
    with pytest.raises(TimeoutError, match="nothing was done"):
        svc.start_run("brew")
    controller.step()  # the controller catches up
    assert svc.current_run() is None
    assert not bench.database.exists()


def test_a_port_in_use_is_an_error_before_any_device_opens(tmp_path: Path, monkeypatch, capsys):
    import socket

    opened: list[str] = []
    monkeypatch.setattr(cli, "lock_path", tmp_path / "openreactor.lock")
    monkeypatch.setattr(cli, "open_port", lambda d: opened.append(d.name))
    config = write_config(tmp_path, password=False)
    with socket.create_server(("127.0.0.1", 0)) as taken:
        port = taken.getsockname()[1]
        assert cli.main(["serve", "-c", config, "--port", str(port)]) == 1
    assert f"error: 127.0.0.1:{port}: Address already in use" in capsys.readouterr().err
    assert opened == []


def test_sigterm_sends_stop_all_at_once_even_with_a_slow_client(tmp_path: Path):
    """A client that never finishes its request must not hold back
    stop-all, nor keep the run from being ended."""
    import json
    import signal
    import socket
    import subprocess
    import sys
    import urllib.request

    config = write_config(tmp_path, password=False)
    with socket.create_server(("127.0.0.1", 0)) as probe:
        port = probe.getsockname()[1]
    launch = (
        "import sys; from pathlib import Path; from openreactor import cli; "
        f"cli.lock_path = Path({str(tmp_path / 'openreactor.lock')!r}); "
        "cli.GRACEFUL_SHUTDOWN_S = 1; sys.exit(cli.main(sys.argv[1:]))"
    )
    server = subprocess.Popen(
        [sys.executable, "-c", launch, "serve", "-c", config, "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:

        def up() -> bool:
            try:
                urllib.request.urlopen(f"{base}/api/v1/status", timeout=1)
                return True
            except OSError:
                return False

        until(up, timeout_s=15)
        request = urllib.request.Request(
            f"{base}/api/v1/runs",
            data=json.dumps({"name": "brew"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        run = json.load(urllib.request.urlopen(request, timeout=5))["id"]
        slow = socket.create_connection(("127.0.0.1", port))
        slow.sendall(b"POST /login HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 1000\r\n\r\nx")
        time.sleep(0.2)
        sent = time.time()
        server.send_signal(signal.SIGTERM)
        server.wait(timeout=10)
        slow.close()
    finally:
        if server.poll() is None:
            server.kill()
    store = Store(tmp_path / "state" / "openreactor.db", read_only=True)
    try:
        assert store.run(run).status == "stopped"  # type: ignore[union-attr]
        db = store._db  # pyright: ignore[reportPrivateUsage]
        [first] = db.execute("SELECT min(time) FROM events WHERE kind = 'stop-all'").fetchone()
    finally:
        store.close()
    assert first - sent < 0.5, server.stderr.read() if server.stderr else ""


def test_a_declared_large_body_is_refused_without_reading_it():
    import asyncio

    from openreactor.web import BodyLimit

    async def app(scope, receive, send):
        raise AssertionError("the app ran")

    async def receive():
        raise AssertionError("the body was read")

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    for length in (str(MAX_BODY_BYTES + 1), "1e9", "-1"):
        sent.clear()
        scope = {"type": "http", "headers": [(b"content-length", length.encode())]}
        asyncio.run(BodyLimit(app)(scope, receive, send))
        assert sent[0]["status"] == 413, length
