"""Serve the HTTP API, ``/api/v1``, over the service layer.

It runs as one process with one worker: the controller lives in the app's
lifespan, and routes are plain ``def`` so FastAPI runs them on its thread
pool and they can wait on the controller without blocking the event loop.

Who may call it:

- No ``password_hash``: only ``serve`` on a loopback address allows this.
  Every request is let in, but only under a loopback ``Host``, so a web page
  cannot reach the API through DNS rebinding.
- With a password: a browser signs in at ``POST /login`` and gets a signed
  session cookie; a script sends ``Authorization: Bearer <password>``. A
  401 never carries a Basic challenge, so a browser never offers its own
  password prompt.

An unsafe request (anything but GET, HEAD and OPTIONS) whose ``Origin``
header names another site is refused. One signed in by cookie must carry a
matching ``Origin``; browsers always send it on such requests, so a missing
one means the request did not come from our own pages.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager, asynccontextmanager
from dataclasses import asdict
from importlib.metadata import version
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi import Path as PathParam
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from openreactor import ui
from openreactor.auth import verify_password
from openreactor.config import Config
from openreactor.dcmt import DcmtStatus
from openreactor.errors import ERRORS, error_detail
from openreactor.rlht import SliceStatus

# web.py does not use MAX_BODY_BYTES itself; tests import it from here.
from openreactor.security import MAX_BODY_BYTES as MAX_BODY_BYTES
from openreactor.security import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_S,
    BodyLimit,
    Busy,
    LoopbackHostsOnly,
    authorize,
    busy,
    check_origin,
    client,
    forbidden,
    same_origin,
)
from openreactor.service import Service

# How long a password check may wait for another client's to finish.
CHECK_WAIT_S = 2.0


class Gate:
    """Check the shared password and keep the signed-in sessions.

    One scrypt check runs at a time: each costs 32 MiB, and a burst of
    wrong guesses must not add up to more. Each client address has at most
    one check running or waiting; another from the same address is refused
    (429) at once. Others wait up to CHECK_WAIT_S for their turn, so one
    guessing host cannot hold the check and lock everyone else out, and a
    password already verified is compared directly without waiting.
    """

    def __init__(self, password_hash: str | None):
        self.password_hash = password_hash
        self._lock = threading.Lock()
        self._clients_lock = threading.Lock()
        self._clients: set[str] = set()
        self._known: bytes | None = None
        # Session ids signed in since the server started; signing out
        # removes one, so a copied cookie stops working.
        self._sessions: set[str] = set()

    @property
    def open(self) -> bool:
        return self.password_hash is None

    def check(self, password: str, client: str = "") -> bool:
        """Return whether ``password`` is the shared password.

        Raises Busy if ``client`` already has a check in hand, or if the
        check stays busy for CHECK_WAIT_S.
        """
        if self.password_hash is None:
            return True
        given = password.encode()
        known = self._known
        if known is not None and hmac.compare_digest(given, known):
            return True
        with self._clients_lock:
            if client in self._clients:
                raise Busy
            self._clients.add(client)
        try:
            if not self._lock.acquire(timeout=CHECK_WAIT_S):
                raise Busy
            try:
                if verify_password(password, self.password_hash):
                    self._known = given
                    return True
                return False
            finally:
                self._lock.release()
        finally:
            with self._clients_lock:
                self._clients.discard(client)

    def sign_in(self) -> str:
        session = secrets.token_urlsafe(32)
        self._sessions.add(session)
        return session

    def signed_in(self, session: object) -> bool:
        return isinstance(session, str) and session in self._sessions

    def sign_out(self, session: object) -> None:
        if isinstance(session, str):
            self._sessions.discard(session)


def service(request: Request) -> Service:
    return request.app.state.service


Svc = Annotated[Service, Depends(service)]


# Request and response bodies


class RunStart(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    notes: str = Field(default="", max_length=10_000)


class RunStarted(BaseModel):
    id: int


class RunStopped(BaseModel):
    id: int
    status: str


class Setpoint(BaseModel):
    # Strict: JSON true or "40" is not a temperature.
    value: float = Field(strict=True, allow_inf_nan=False)


class Calibration(BaseModel):
    point: str
    value: float | None = Field(default=None, allow_inf_nan=False)


class CalibrationStatus(BaseModel):
    device: str
    status: str


class DeviceOut(BaseModel):
    name: str
    kind: str
    status: str


class StatusOut(BaseModel):
    version: str
    run: int | None
    # Why the current run stopped recording, if it did; it stays the
    # current run until it is stopped.
    recording_failed: str | None
    devices: list[DeviceOut]
    # Each RLHT slice's firmware, mode, e-stop, watchdog and outputs.
    slices: list[SliceStatus]
    # Each DCMT slice's firmware, mode, e-stop, watchdog and motors.
    dcmt_slices: list[DcmtStatus]


class ChannelOut(BaseModel):
    name: str
    device: str
    kind: str
    unit: str
    value: float | None
    time: float | None
    outcome: str


class RunOut(BaseModel):
    id: int
    name: str
    notes: str
    started: float
    ended: float | None
    status: str


class EventOut(BaseModel):
    time: float
    source: str
    kind: str
    device: str | None
    channel: str | None
    details: str
    result: str


api = APIRouter(prefix="/api/v1", dependencies=[Depends(authorize)])


@api.get("/status")
def get_status(svc: Svc) -> StatusOut:
    devices = [DeviceOut(**asdict(d)) for d in svc.devices()]
    return StatusOut(
        version=version("openreactor"),
        run=svc.current_run(),
        recording_failed=svc.recording_failed(),
        devices=devices,
        slices=svc.slices(),
        dcmt_slices=svc.dcmt_slices(),
    )


@api.get("/channels")
def get_channels(svc: Svc) -> list[ChannelOut]:
    return [ChannelOut(**asdict(c)) for c in svc.channels()]


@api.put("/channels/{channel}/setpoint", status_code=204)
def put_setpoint(channel: str, body: Setpoint, svc: Svc) -> None:
    svc.set_setpoint(channel, body.value)


@api.post("/stop-all")
def post_stop_all(svc: Svc) -> list[EventOut]:
    return [EventOut(**asdict(e)) for e in svc.stop_all()]


@api.post("/devices/{device}/clear-trip")
def post_clear_trip(device: str, svc: Svc) -> list[EventOut]:
    return [EventOut(**asdict(e)) for e in svc.clear_trip(device)]


@api.get("/runs")
def get_runs(svc: Svc) -> list[RunOut]:
    return [RunOut(**asdict(r)) for r in svc.runs()]


@api.post("/runs", status_code=201)
def post_run(body: RunStart, svc: Svc) -> RunStarted:
    return RunStarted(id=svc.start_run(body.name, body.notes))


@api.post("/runs/current/stop")
def post_run_stop(svc: Svc) -> RunStopped:
    run, status = svc.stop_run()
    return RunStopped(id=run, status=status)


@api.get(
    "/runs/{run}/export",
    response_class=Response,
    responses={200: {"content": {"application/zip": {}}}},
)
def get_export(run: Annotated[int, PathParam(ge=1, le=2**63 - 1)], svc: Svc) -> Response:
    return Response(
        svc.export(run),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="run-{run}.zip"'},
    )


@api.get("/ezo/{device}/calibration")
def get_calibration(device: str, svc: Svc) -> CalibrationStatus:
    return CalibrationStatus(device=device, status=svc.calibration(device))


@api.post("/ezo/{device}/calibration")
def post_calibration(device: str, body: Calibration, svc: Svc) -> CalibrationStatus:
    return CalibrationStatus(device=device, status=svc.calibrate(device, body.point, body.value))


@api.delete("/ezo/{device}/calibration")
def delete_calibration(device: str, svc: Svc) -> CalibrationStatus:
    return CalibrationStatus(device=device, status=svc.clear_calibration(device))


def wants_html(request: Request) -> bool:
    """Return whether a browser submitted a form, rather than a script or htmx."""
    return "text/html" in request.headers.get("accept", "") and "hx-request" not in request.headers


async def _password_from(request: Request) -> str:
    body = await request.body()
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            data = json.loads(body)
        except ValueError:
            return ""
        password = data.get("password") if isinstance(data, dict) else None
        return password if isinstance(password, str) else ""
    return parse_qs(body.decode("utf-8", "replace")).get("password", [""])[0]


def create_app(config: Config, start: Callable[[], AbstractContextManager[Service]]) -> FastAPI:
    """Build the app.

    ``start``, such as ``service.running``, opens the circuits and starts
    the controller, and on exit sends stop-all and closes them.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with start() as svc:
            app.state.service = svc
            # The server's signal handler sends stop-all with this at once,
            # before it waits for open connections to finish.
            app.state.stop_now = svc.stop_now
            try:
                yield
            finally:
                app.state.stop_now = None

    app = FastAPI(
        title="openreactor",
        version=version("openreactor"),
        lifespan=lifespan,
        # The docs sit behind authorization; see below.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.gate = Gate(config.server.password_hash)
    app.state.stop_now = None
    # A new secret each start: a restart signs everyone out.
    app.add_middleware(
        SessionMiddleware,
        secret_key=secrets.token_urlsafe(32),
        session_cookie=SESSION_COOKIE,
        max_age=SESSION_MAX_AGE_S,
        same_site="strict",
    )

    app.include_router(api)
    if app.state.gate.open:
        app.add_middleware(LoopbackHostsOnly)
    # Added after the session, so it runs ahead of it and of every route.
    # Only NoFraming, which ui.install adds, sits outside it.
    app.add_middleware(BodyLimit)

    @app.get("/api/v1/openapi.json", include_in_schema=False, dependencies=[Depends(authorize)])
    def openapi() -> dict[str, Any]:
        return app.openapi()

    @app.post("/login", status_code=204, include_in_schema=False)
    async def login(request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin is not None and not same_origin(request, origin):
            raise forbidden(f"cross-site request from {origin} refused")
        password = await _password_from(request)
        gate: Gate = app.state.gate
        # scrypt is slow on purpose; keep it off the event loop.
        try:
            ok = await run_in_threadpool(gate.check, password, client(request))
        except Busy:
            raise busy() from None
        if not ok:
            if wants_html(request):
                return ui.login_page(request, error="Wrong password.", status_code=401)
            raise HTTPException(status_code=401, detail="wrong password")
        gate.sign_out(request.session.get("id"))
        request.session.clear()
        request.session["id"] = gate.sign_in()
        if wants_html(request):
            return RedirectResponse("/", status_code=303)
        return Response(status_code=204)

    @app.post("/logout", status_code=204, include_in_schema=False)
    def logout(request: Request) -> Response:
        # Another site, or another service on this host, cannot sign you out.
        session = request.session.get("id")
        check_origin(request, by_cookie=app.state.gate.signed_in(session))
        app.state.gate.sign_out(session)
        request.session.clear()
        if wants_html(request):
            return RedirectResponse("/login", status_code=303)
        return Response(status_code=204)

    for error, code in ERRORS:

        def handler(request: Request, exc: Exception, code: int = code) -> JSONResponse:
            return JSONResponse({"detail": error_detail(exc)}, status_code=code)

        app.add_exception_handler(error, handler)

    ui.install(app)
    return app
