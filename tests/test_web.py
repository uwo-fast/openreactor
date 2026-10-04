import errno
import io
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fakes import FakeActuator, FakeI2cBus, FakePort, FakeRlht
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
channels.jacket = { output = 1, tc = 1, max_setpoint = 80 }

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
        # The config's RLHT, answering on a fake bus.
        self.rlht = FakeRlht()
        self.rlht_address = 0x0A  # where it answers; the config says 0x0A
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
                open_bus=lambda path: FakeI2cBus({self.rlht_address: self.rlht}),
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
        {"name": "heater", "kind": "rlht", "status": "ok"},
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
    until(lambda: len(client.get("/api/v1/channels").json()) == 5)
    channels = {c["name"]: c for c in client.get("/api/v1/channels").json()}
    assert channels["air.humidity"]["value"] == 41.0
    assert channels["air.humidity"]["unit"] == "%"
    assert channels["air.temperature"]["outcome"] == "ok"
    # The RLHT's channel, from its GET_STATE poll.
    assert (channels["jacket.temperature"]["value"], channels["jacket.temperature"]["unit"]) == (
        25.1,
        "°C",
    )
    assert channels["jacket.temperature"]["device"] == "heater"


def test_stop_all_returns_its_events(open_bench):
    bench, client = open_bench
    events = client.post("/api/v1/stop-all").json()
    assert [(e["kind"], e["device"], e["result"]) for e in events] == [
        ("stop-all", "jacket", "ok"),
        ("stop-all", "heater", "ok"),
    ]
    assert bench.log == ["safe jacket"]


def jacket_setpoint(client: TestClient) -> float | None:
    for c in client.get("/api/v1/channels").json():
        if c["name"] == "jacket.setpoint":
            return c["value"]
    return None


def test_a_setpoint_reaches_the_slice_and_is_recorded(open_bench):
    bench, client = open_bench
    run = client.post("/api/v1/runs", json={"name": "heat"}).json()["id"]
    r = client.put("/api/v1/channels/jacket/setpoint", json={"value": 37.06})
    assert r.status_code == 204
    # Rounded to the slice's tenth of a degree, on output 1, output 2 left at 0.
    assert bench.rlht.setpoints == [371, 0]
    until(lambda: jacket_setpoint(client) == 37.1)
    client.post("/api/v1/runs/current/stop")
    export = client.get(f"/api/v1/runs/{run}/export")
    with zipfile.ZipFile(io.BytesIO(export.content)) as z:
        events = z.read("events.csv").decode()
    assert "setpoint" in events and "37.1 °C" in events and "jacket" in events


def test_a_setpoint_is_checked_before_it_is_sent(open_bench):
    bench, client = open_bench

    def put(value: object) -> tuple[int, str]:
        r = client.put("/api/v1/channels/jacket/setpoint", json={"value": value})
        return r.status_code, r.text

    bench.rlht.commands.clear()
    assert put(-1)[0] == 422
    code, text = put(80.1)
    assert code == 422 and "above its max_setpoint, 80 °C" in text
    assert put("hot")[0] == 422
    assert client.put("/api/v1/channels/nothing/setpoint", json={"value": 1}).status_code == 404
    assert bench.rlht.commands == []
    assert put(80)[0] == 204
    assert bench.rlht.setpoints == [800, 0]


def test_a_setpoint_must_be_a_json_number_and_is_checked_as_sent(open_bench):
    bench, client = open_bench

    def put(value: object) -> int:
        return client.put("/api/v1/channels/jacket/setpoint", json={"value": value}).status_code

    assert put(True) == 422
    assert put("40") == 422
    # 80.06 would go to the slice as 80.1, above max_setpoint = 80.
    assert put(80.06) == 422
    assert put(80.04) == 204
    assert bench.rlht.setpoints == [800, 0]


def test_a_failed_send_is_an_error_event_and_a_502(open_bench):
    bench, client = open_bench
    run = client.post("/api/v1/runs", json={"name": "heat"}).json()["id"]
    bench.rlht.fail_after[0x02] = OSError(errno.ETIMEDOUT, "Connection timed out")
    r = client.put("/api/v1/channels/jacket/setpoint", json={"value": 70})
    assert r.status_code == 502
    assert "it may have reached the slice" in r.json()["detail"]
    # It did: the polls find the slice running what nobody wants, and stop it.
    del bench.rlht.fail_after[0x02]
    until(lambda: bench.rlht.setpoints == [0, 0])
    client.post("/api/v1/runs/current/stop")
    export = client.get(f"/api/v1/runs/{run}/export")
    with zipfile.ZipFile(io.BytesIO(export.content)) as z:
        events = z.read("events.csv").decode()
    assert "error: heater: [Errno 110] Connection timed out" in events
    assert "slice-setpoints-changed" in events


def test_a_setpoint_for_a_slice_that_never_answered_is_refused(tmp_path: Path):
    bench = Bench(tmp_path, password=False)
    bench.rlht_address = 0x0B  # nothing at the config's 0x0A
    with TestClient(bench.app, base_url=LOCAL) as client:
        r = client.put("/api/v1/channels/jacket/setpoint", json={"value": 37})
    assert r.status_code == 503
    assert "heater is not in use: no answer on the bus" in r.json()["detail"]


def test_serve_prints_start_up_and_events_but_not_readings(open_bench):
    bench, client = open_bench
    client.put("/api/v1/channels/jacket/setpoint", json={"value": 37})
    until(lambda: any(getattr(e, "kind", "") == "setpoint" for e in bench.printed))
    kinds = [getattr(e, "kind", None) for e in bench.printed]
    assert kinds[0] == "slice-start"
    assert all(k is not None for k in kinds), "a reading was printed"


def test_a_setpoint_for_an_unreachable_slice_is_refused(open_bench):
    bench, client = open_bench
    bench.rlht.faults = [OSError(errno.EREMOTEIO, "Remote I/O error")] * 10_000
    until(lambda: "unreachable" in str(client.get("/api/v1/status").json()["devices"]))
    r = client.put("/api/v1/channels/jacket/setpoint", json={"value": 37})
    assert r.status_code == 503
    assert "unreachable" in r.json()["detail"]


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


def test_one_guessing_client_cannot_lock_out_another(monkeypatch):
    import threading

    from openreactor import web
    from openreactor.web import Busy, Gate

    gate = Gate(HASH)
    real = web.verify_password
    entered, release = threading.Event(), threading.Event()

    def slow_verify(password: str, stored: str) -> bool:
        if password == "wrong":
            entered.set()
            release.wait(5)
            return False
        return real(password, stored)

    monkeypatch.setattr(web, "verify_password", slow_verify)
    guess = threading.Thread(target=lambda: gate.check("wrong", "10.0.0.66"))
    guess.start()
    assert entered.wait(5)
    # The guessing host gets no second check in hand, nor a wait for one.
    started = time.monotonic()
    with pytest.raises(Busy):
        gate.check("also wrong", "10.0.0.66")
    assert time.monotonic() - started < 0.5
    # Another host waits its turn instead of being turned away.
    results: list[bool] = []
    user = threading.Thread(target=lambda: results.append(gate.check(PASSWORD, "10.0.0.7")))
    user.start()
    time.sleep(0.2)
    assert results == []
    release.set()
    user.join(5)
    guess.join(5)
    assert results == [True]
    # Once verified, the password is let in without waiting.
    assert gate.check(PASSWORD, "10.0.0.66")


def test_a_check_that_stays_busy_gives_up(monkeypatch):
    import threading

    from openreactor import web
    from openreactor.web import Busy, Gate

    monkeypatch.setattr(web, "CHECK_WAIT_S", 0.1)
    gate = Gate(HASH)
    entered, release = threading.Event(), threading.Event()

    def slow_verify(password: str, stored: str) -> bool:
        entered.set()
        release.wait(5)
        return False

    monkeypatch.setattr(web, "verify_password", slow_verify)
    guess = threading.Thread(target=lambda: gate.check("wrong", "a"))
    guess.start()
    assert entered.wait(5)
    with pytest.raises(Busy):
        gate.check(PASSWORD, "b")
    release.set()
    guess.join(5)


def test_a_busy_gate_answers_429(locked_bench, monkeypatch):
    from openreactor.web import Busy

    bench, client = locked_bench

    def busy(password: str, client: str = "") -> bool:
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


def start_server(tmp_path: Path, config: str):
    import socket
    import subprocess
    import sys
    import urllib.request

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

    def up() -> bool:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/status", timeout=1)
            return True
        except OSError:
            return False

    until(up, timeout_s=15)
    return server, port


def test_ctrl_c_stops_serve_cleanly(tmp_path: Path):
    import signal

    server, _ = start_server(tmp_path, write_config(tmp_path, password=False))
    server.send_signal(signal.SIGINT)
    _, err = server.communicate(timeout=10)
    assert server.returncode == 0, err
    assert "Traceback" not in err
    assert "Application shutdown complete" in err


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


def test_a_slice_that_failed_start_up_is_not_polled_but_gets_stop_all(tmp_path: Path):
    bench = Bench(tmp_path, password=False)
    bench.rlht.arms = False
    with TestClient(bench.app, base_url=LOCAL) as client:
        status = {d["name"]: d["status"] for d in client.get("/api/v1/status").json()["devices"]}
        assert "watchdog did not arm" in status["heater"]
        time.sleep(0.3)  # several slice polls at the bench's 20x clock
        assert bench.rlht.state_replies == 0
        bench.rlht.commands.clear()
        events = client.post("/api/v1/stop-all").json()
    assert ("heater", "ok") in [(e["device"], e["result"]) for e in events]
    assert [op for op, _ in bench.rlht.commands][:2] == [0x02, 0x06]


def test_status_shows_a_slice_e_stop_and_stop_all_reaches_everything(open_bench):
    bench, client = open_bench
    bench.rlht.flags = 0x01  # e-stop pressed on the slice
    until(lambda: "e-stop" in str(client.get("/api/v1/status").json()["devices"]))
    status = {d["name"]: d["status"] for d in client.get("/api/v1/status").json()["devices"]}
    assert status["heater"] == "e-stop held on the slice"
    assert "safe jacket" in bench.log  # stop-all reached the other actuator too
    # Nothing restarts heating while it is held.
    r = client.put("/api/v1/channels/jacket/setpoint", json={"value": 37})
    assert r.status_code == 409
    assert "e-stop on heater is held" in r.json()["detail"]
    assert bench.rlht.setpoints == [0, 0]


def slice_status(client: TestClient) -> dict:
    return client.get("/api/v1/status").json()["slices"][0]


def test_status_shows_each_slice_as_the_controller_sees_it(open_bench):
    bench, client = open_bench
    s = slice_status(client)
    assert (s["name"], s["state"], s["version"]) == ("heater", "ok", "RLHT 1.0.0, CRUMBS 0.15.0")
    assert s["mode"] == "closed loop" and s["estop"] is False
    assert s["watchdog"] == {"armed": True, "timeout_ms": 5000, "tripped": False, "trip_count": 0}
    assert [o["channel"] for o in s["outputs"]] == ["jacket", None]
    # The watchdog as the latest check read it.
    bench.rlht.trip()
    until(lambda: slice_status(client)["watchdog"]["trip_count"] == 1)
    bench.rlht.flags = 0x01
    until(lambda: slice_status(client)["state"] == "e-stop held on the slice")
    bench.rlht.flags = 0
    until(lambda: slice_status(client)["state"] == "ok")
    bench.rlht.faults = [OSError(errno.EREMOTEIO, "Remote I/O error")] * 10_000
    until(lambda: slice_status(client)["state"].startswith("unreachable"))


def test_status_shows_a_slice_that_never_answered_or_did_not_start(tmp_path: Path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    bench = Bench(tmp_path / "a", password=False)
    bench.rlht_address = 0x0B
    with TestClient(bench.app, base_url=LOCAL) as client:
        s = slice_status(client)
    assert s["state"] == "error: no answer on the bus" and s["version"] is None
    bench = Bench(tmp_path / "b", password=False)
    bench.rlht.arms = False
    with TestClient(bench.app, base_url=LOCAL) as client:
        s = slice_status(client)
    assert "watchdog did not arm" in s["state"] and s["version"] == "RLHT 1.0.0, CRUMBS 0.15.0"


def test_status_survives_a_busy_controller(open_bench, monkeypatch: pytest.MonkeyPatch):
    from openreactor import service as service_module

    bench, client = open_bench
    monkeypatch.setattr(service_module, "SLICE_STATUS_TIMEOUT_S", 0.2)
    svc = bench.app.state.service
    svc.controller.call(lambda: time.sleep(1.0))  # the controller is busy
    r = client.get("/api/v1/status")
    assert r.status_code == 200
    [s] = r.json()["slices"]
    assert s["state"] == "unknown: the controller did not answer in time"
    assert {d["name"] for d in r.json()["devices"]} == {"heater", "air"}
