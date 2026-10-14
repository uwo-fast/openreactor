import asyncio
import errno
import io
import json
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

import pytest
from conftest import HASH, LOCAL, PASSWORD, Bench, config_text, until
from fakes import SET_SETPOINTS
from fastapi.testclient import TestClient

from openreactor import cli, service, web
from openreactor.auth import verify_password
from openreactor.controller import Controller
from openreactor.ezo import EzoDeviceError, EzoReader, Outcome
from openreactor.lock import ControllerLock
from openreactor.service import Service
from openreactor.storage import Store
from openreactor.web import MAX_BODY_BYTES, SESSION_COOKIE, BodyLimit, Busy, Gate


def sign_in(client: TestClient) -> None:
    r = client.post("/login", data={"password": PASSWORD}, headers={"Origin": LOCAL})
    assert r.status_code == 204
    assert SESSION_COOKIE in client.cookies


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
        {"name": "motors", "kind": "dcmt", "status": "ok"},
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
    until(lambda: len(client.get("/api/v1/channels").json()) == 6)
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
        ("stop-all", "motors", "ok"),
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


# DCMT motors


def stirrer_duty(client: TestClient) -> float | None:
    for c in client.get("/api/v1/channels").json():
        if c["name"] == "stirrer.duty":
            return c["value"]
    return None


def test_a_motor_duty_reaches_the_slice_and_is_recorded(open_bench):
    bench, client = open_bench
    client.post("/api/v1/runs", json={"name": "stir"})
    r = client.put("/api/v1/channels/stirrer/setpoint", json={"value": 50})
    assert r.status_code == 204
    assert (bench.dcmt.pwm, bench.dcmt.brakes) == ([128, 0], [0, 1])
    until(lambda: stirrer_duty(client) == 50.2)
    run = client.post("/api/v1/runs/current/stop").json()["id"]
    with zipfile.ZipFile(io.BytesIO(client.get(f"/api/v1/runs/{run}/export").content)) as z:
        events = z.read("events.csv").decode()
        readings = z.read("readings.csv").decode()
    assert "setpoint,motors,stirrer,50.2 %,ok" in events
    assert ",stirrer.duty,50.2,%" in readings


@pytest.mark.parametrize("value", [100.5, -101, True, "50"])
def test_a_motor_duty_outside_minus_100_to_100_is_refused(open_bench, value):
    bench, client = open_bench
    r = client.put("/api/v1/channels/stirrer/setpoint", json={"value": value})
    assert r.status_code == 422
    assert bench.dcmt.pwm == [0, 0]


def test_a_reverse_duty_and_zero_brake_the_right_motor(open_bench):
    bench, client = open_bench
    assert client.put("/api/v1/channels/stirrer/setpoint", json={"value": -100}).status_code == 204
    assert bench.dcmt.pwm == [-255, 0]
    assert client.put("/api/v1/channels/stirrer/setpoint", json={"value": 0}).status_code == 204
    assert (bench.dcmt.pwm, bench.dcmt.brakes) == ([0, 0], [1, 1])


def test_status_shows_each_dcmt_slice(open_bench):
    bench, client = open_bench
    client.put("/api/v1/channels/stirrer/setpoint", json={"value": 100})
    until(
        lambda: (
            client.get("/api/v1/status").json()["dcmt_slices"][0]["motors"][0]["duty_pct"] == 100.0
        )
    )
    [s] = client.get("/api/v1/status").json()["dcmt_slices"]
    assert (s["name"], s["address"], s["state"], s["mode"]) == ("motors", 20, "ok", "open loop")
    assert s["watchdog"] == {"armed": True, "timeout_ms": 5000, "tripped": False, "trip_count": 0}
    assert s["motors"] == [
        {"motor": 1, "channel": "stirrer", "duty_pct": 100.0, "brake": False},
        {"motor": 2, "channel": None, "duty_pct": 0.0, "brake": True},
    ]
    assert s["desired_pct"] == [100.0, 0.0]


def held(client: TestClient) -> bool:
    devices = {d["name"]: d["status"] for d in client.get("/api/v1/status").json()["devices"]}
    return devices["motors"] == "watchdog trip held: clear it to drive the motors"


def test_a_held_trip_is_cleared_only_when_asked_and_resumes_nothing(open_bench):
    bench, client = open_bench
    client.put("/api/v1/channels/stirrer/setpoint", json={"value": 50})
    bench.dcmt.trip()
    until(lambda: held(client))
    r = client.put("/api/v1/channels/stirrer/setpoint", json={"value": 50})
    assert r.status_code == 409 and "clear it first" in r.json()["detail"]
    assert bench.dcmt.tripped == 1
    r = client.post("/api/v1/devices/motors/clear-trip")
    assert r.status_code == 200
    [event] = r.json()
    assert (event["kind"], event["source"], event["device"]) == (
        "slice-trip-cleared",
        "user",
        "motors",
    )
    assert (bench.dcmt.tripped, bench.dcmt.pwm, bench.dcmt.brakes) == (0, [0, 0], [1, 1])
    until(lambda: not held(client))
    assert client.put("/api/v1/channels/stirrer/setpoint", json={"value": 20}).status_code == 204


def test_clear_trip_is_refused_with_nothing_held_or_on_another_device(open_bench):
    bench, client = open_bench
    r = client.post("/api/v1/devices/motors/clear-trip")
    assert r.status_code == 409 and "no watchdog trip is held" in r.json()["detail"]
    for device in ("heater", "air", "nothing"):
        assert client.post(f"/api/v1/devices/{device}/clear-trip").status_code == 404
    assert bench.dcmt.commands[-1][0] != 0x7C


def test_a_duty_is_refused_through_the_api_while_the_e_stop_is_held(open_bench):
    bench, client = open_bench
    bench.dcmt.press_estop()
    until(lambda: client.get("/api/v1/status").json()["dcmt_slices"][0]["estop"])
    r = client.put("/api/v1/channels/stirrer/setpoint", json={"value": 50})
    assert r.status_code == 409 and "e-stop" in r.json()["detail"]
    assert bench.dcmt.pwm == [0, 0]


def test_clear_trip_answers_502_when_the_send_fails(open_bench):
    bench, client = open_bench
    bench.dcmt.trip()
    until(lambda: held(client))
    bench.dcmt.fail_opcodes[0x7C] = OSError(5, "I/O error")
    r = client.post("/api/v1/devices/motors/clear-trip")
    assert r.status_code == 502 and "may or may not be cleared" in r.json()["detail"]
    assert bench.dcmt.tripped == 1


def test_clear_trip_answers_503_for_a_dcmt_that_did_not_start(tmp_path: Path):
    bench = Bench(tmp_path, password=False)
    bench.dcmt.caps = 0x74  # no open loop or brakes: refused at start-up
    with TestClient(bench.app, base_url=LOCAL) as client:
        r = client.post("/api/v1/devices/motors/clear-trip")
        assert r.status_code == 503 and "did not finish start-up" in r.json()["detail"]
        [s] = client.get("/api/v1/status").json()["dcmt_slices"]
        assert "lack open-loop control or brakes" in s["state"]
        assert s["desired_pct"] is None


def test_a_cross_site_clear_trip_is_refused(open_bench):
    bench, client = open_bench
    bench.dcmt.trip()
    until(lambda: held(client))
    evil = {"Origin": "http://evil.example"}
    assert client.post("/api/v1/devices/motors/clear-trip", headers=evil).status_code == 403
    hx = {"HX-Request": "true", **evil}
    assert client.post("/ui/devices/motors/clear-trip", headers=hx).status_code == 403
    assert bench.dcmt.tripped == 1


def test_no_release_when_the_controller_does_not_stop_in_time(tmp_path: Path):
    """A controller still running could drive a motor after its watchdog
    was disarmed: leave the watchdog armed, to trip and brake it."""
    bench = Bench(tmp_path, password=False)
    with pytest.raises(TimeoutError), TestClient(bench.app, base_url=LOCAL):
        svc = bench.app.state.service
        close = svc.controller.close

        def stuck() -> None:
            close()  # stop it for real, then report the timeout
            raise TimeoutError("the controller did not stop in time")

        svc.controller.close = stuck
    assert bench.dcmt.armed == 1
    assert (0x7E, b"\x00\x00") not in bench.dcmt.commands


def test_the_dcmt_is_released_even_if_ending_the_run_fails(tmp_path: Path):
    bench = Bench(tmp_path, password=False)
    with pytest.raises(sqlite3.OperationalError), TestClient(bench.app, base_url=LOCAL) as client:
        client.post("/api/v1/runs", json={"name": "r"})
        store = bench.app.state.service._store

        def fail(run: int, status: str) -> None:
            raise sqlite3.OperationalError("disk I/O error")

        store.end_run = fail
    assert (bench.dcmt.armed, bench.dcmt.brakes) == (0, [1, 1])


def test_a_clean_shutdown_releases_the_dcmt(tmp_path: Path):
    bench = Bench(tmp_path, password=False)
    with TestClient(bench.app, base_url=LOCAL) as client:
        client.put("/api/v1/channels/stirrer/setpoint", json={"value": 50})
        assert bench.dcmt.armed == 1
    # Stop-all, then the safe state and the watchdog disarmed, so a latching
    # slice is not left to trip once nothing polls it.
    assert (bench.dcmt.pwm, bench.dcmt.brakes, bench.dcmt.armed) == ([0, 0], [1, 1], 0)


# Profiles

PROFILE = """
version = 1
name = "warm and stir"
[channels]
jacket = [{ set = 30.0 }, { ramp = 35.0, over = "2s" }, { hold = "%s" }]
stirrer = [{ set = 40.0 }, { hold = "%s" }]
"""


def run_status(client: TestClient, run: int) -> str:
    return next(r["status"] for r in client.get("/api/v1/runs").json() if r["id"] == run)


def test_a_profile_runs_to_its_end_in_its_own_run_and_ends_safe(open_bench):
    bench, client = open_bench
    r = client.post("/api/v1/profile", json={"profile": PROFILE % ("3s", "5s")})
    assert r.status_code == 201
    run = r.json()["id"]
    until(lambda: bench.rlht.setpoints[0] > 0)
    state = client.get("/api/v1/profile").json()
    assert (state["run"], state["name"], state["duration_s"]) == (run, "warm and stir", 5.0)
    until(lambda: run_status(client, run) == "completed")
    assert client.get("/api/v1/profile").json() is None
    assert bench.rlht.setpoints == [0, 0] and bench.dcmt.pwm == [0, 0]
    with zipfile.ZipFile(io.BytesIO(client.get(f"/api/v1/runs/{run}/export").content)) as z:
        meta = json.loads(z.read("run.json"))
        events = z.read("events.csv").decode()
    assert meta["name"] == "warm and stir" and "ramp = 35.0" in meta["profile"]
    assert "profile,setpoint,heater,jacket,30 °C,ok" in events
    assert "profile-end" in events


def test_an_aborted_profile_sends_its_channels_safe_and_stops_its_run(open_bench):
    bench, client = open_bench
    run = client.post("/api/v1/profile", json={"profile": PROFILE % ("60s", "60s")}).json()["id"]
    until(lambda: bench.dcmt.pwm[0] != 0)
    r = client.post("/api/v1/profile/abort")
    assert r.json() == {"id": run, "status": "stopped"}
    assert bench.rlht.setpoints == [0, 0] and bench.dcmt.pwm == [0, 0]
    assert client.post("/api/v1/profile/abort").status_code == 409


@pytest.mark.parametrize("how", ["stop-all", "stop the run"])
def test_stop_all_or_stopping_the_run_aborts_the_profile(open_bench, how):
    bench, client = open_bench
    run = client.post("/api/v1/profile", json={"profile": PROFILE % ("60s", "60s")}).json()["id"]
    until(lambda: bench.dcmt.pwm[0] != 0)
    if how == "stop-all":
        client.post("/api/v1/stop-all")
    else:
        assert client.post("/api/v1/runs/current/stop").json()["status"] == "stopped"
    until(lambda: run_status(client, run) == "stopped")
    assert client.get("/api/v1/profile").json() is None
    with zipfile.ZipFile(io.BytesIO(client.get(f"/api/v1/runs/{run}/export").content)) as z:
        events = z.read("events.csv").decode()
    reason = "stop-all was sent" if how == "stop-all" else "the run was stopped"
    assert f"profile-abort,,,{reason},aborted" in events
    time.sleep(0.3)  # several profile steps: nothing is sent again
    assert bench.rlht.setpoints == [0, 0] and bench.dcmt.pwm == [0, 0]


def test_a_profile_on_a_slice_that_did_not_start_is_refused(tmp_path: Path):
    bench = Bench(tmp_path, password=False)
    bench.dcmt.caps = 0x74
    with TestClient(bench.app, base_url=LOCAL) as client:
        r = client.post("/api/v1/profile", json={"profile": PROFILE % ("60s", "60s")})
        assert r.status_code == 503 and "stirrer: motors is not in use" in r.json()["detail"]
        assert client.get("/api/v1/runs").json() == []


def test_a_profile_on_an_unreachable_slice_is_refused(open_bench):
    bench, client = open_bench
    bench.dcmt.faults = [OSError(121, "Remote I/O error")] * 100000
    until(lambda: client.get("/api/v1/status").json()["dcmt_slices"][0]["state"].startswith("unr"))
    r = client.post("/api/v1/profile", json={"profile": PROFILE % ("60s", "60s")})
    assert r.status_code == 503 and "stirrer: motors is unreachable" in r.json()["detail"]
    bench.dcmt.faults = []


def test_a_shutdown_during_a_profile_ends_its_run_interrupted(tmp_path: Path):
    bench = Bench(tmp_path, password=False)
    with TestClient(bench.app, base_url=LOCAL) as client:
        run = client.post("/api/v1/profile", json={"profile": PROFILE % ("60s", "60s")})
        run = run.json()["id"]
        until(lambda: bench.dcmt.pwm[0] != 0)
    store = Store(bench.database, read_only=True)
    try:
        info = store.run(run)
        assert info is not None and info.status == "interrupted"
    finally:
        store.close()
    assert bench.rlht.setpoints == [0, 0] and bench.dcmt.pwm == [0, 0]


def test_a_bad_profile_is_refused_with_every_problem(open_bench):
    _, client = open_bench
    text = 'version = 1\nname = "x"\n[channels]\njacket = [{ set = 90.0 }, { hold = "1m" }]\n'
    text += 'nothing = [{ set = 1.0 }, { hold = "1m" }]\n'
    r = client.post("/api/v1/profile", json={"profile": text})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "channels.nothing: is not an actuated channel in the config" in detail
    assert "channels.jacket[0]: 90 is outside what the channel takes, 0 to 80" in detail
    assert client.get("/api/v1/runs").json() == []


def test_a_profile_is_refused_while_a_run_records(open_bench):
    _, client = open_bench
    client.post("/api/v1/runs", json={"name": "manual"})
    r = client.post("/api/v1/profile", json={"profile": PROFILE % ("60s", "60s")})
    assert r.status_code == 409 and "already recording" in r.json()["detail"]
    client.post("/api/v1/runs/current/stop")
    assert (
        client.post("/api/v1/profile", json={"profile": PROFILE % ("60s", "60s")}).status_code
        == 201
    )
    r = client.post("/api/v1/profile", json={"profile": PROFILE % ("60s", "60s")})
    assert r.status_code == 409  # a second profile, while the first one's run records


def test_a_setpoint_by_hand_on_a_profiles_channel_is_refused(open_bench):
    bench, client = open_bench
    run = client.post("/api/v1/profile", json={"profile": PROFILE % ("60s", "60s")}).json()["id"]
    until(lambda: bench.rlht.setpoints[0] > 0)
    r = client.put("/api/v1/channels/jacket/setpoint", json={"value": 20})
    assert r.status_code == 409 and "abort the profile first" in r.json()["detail"]
    assert bench.rlht.setpoints[0] != 200
    assert run_status(client, run) == "running"


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
    bench.rlht.fail_after[SET_SETPOINTS] = OSError(errno.ETIMEDOUT, "Connection timed out")
    r = client.put("/api/v1/channels/jacket/setpoint", json={"value": 70})
    assert r.status_code == 502
    assert "it may have reached the slice" in r.json()["detail"]
    # It did: the polls find the slice running what nobody wants, and stop it.
    del bench.rlht.fail_after[SET_SETPOINTS]
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
    bench = Bench(tmp_path, password=False)
    bench.port.family_error = EzoDeviceError("cannot open /dev/i2c-1 at 0x6F: no reply")
    with TestClient(bench.app, base_url=LOCAL) as client:
        r = client.get("/api/v1/ezo/air/calibration")
        assert r.status_code == 409
        assert r.json()["detail"].startswith("air is out of use since startup")
        status = client.get("/api/v1/status").json()
        assert status["devices"][2] == {
            "name": "air",
            "kind": "ezo-hum",
            "status": "error: cannot open /dev/i2c-1 at 0x6F: no reply",
        }


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
    server, _ = start_server(tmp_path, write_config(tmp_path, password=False))
    server.send_signal(signal.SIGINT)
    _, err = server.communicate(timeout=10)
    assert server.returncode == 0, err
    assert "Traceback" not in err
    assert "Application shutdown complete" in err


def test_sigterm_sends_stop_all_at_once_even_with_a_slow_client(tmp_path: Path):
    """A client that never finishes its request must not hold back
    stop-all, nor keep the run from being ended."""
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
    bench, client = open_bench
    monkeypatch.setattr(service, "SLICE_STATUS_TIMEOUT_S", 0.2)
    svc = bench.app.state.service
    svc.controller.call(lambda: time.sleep(1.0))  # the controller is busy
    r = client.get("/api/v1/status")
    assert r.status_code == 200
    [s] = r.json()["slices"]
    assert s["state"] == "unknown: the controller did not answer in time"
    assert {d["name"] for d in r.json()["devices"]} == {"heater", "motors", "air"}
