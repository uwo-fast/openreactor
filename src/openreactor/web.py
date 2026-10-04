"""The HTTP API, ``/api/v1``, over the service layer.

One process, one worker: the controller lives in the app's lifespan, and
routes are plain ``def`` so FastAPI runs them on its thread pool and they
can wait on the controller without blocking the event loop.

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
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import AbstractContextManager, asynccontextmanager
from dataclasses import asdict
from importlib.metadata import version
from typing import Annotated, Any
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from openreactor.auth import verify_password
from openreactor.config import Config
from openreactor.controller import ControllerClosed
from openreactor.service import Conflict, Invalid, NotFound, Service, Unavailable

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
SESSION_COOKIE = "openreactor_session"
SESSION_MAX_AGE_S = 12 * 60 * 60


class Gate:
    """Checks the shared password. One check runs at a time: each costs
    32 MiB of scrypt, and a burst of wrong guesses must not add up to more."""

    def __init__(self, password_hash: str | None):
        self.password_hash = password_hash
        self._lock = threading.Lock()
        self._known: bytes | None = None

    @property
    def open(self) -> bool:
        return self.password_hash is None

    def check(self, password: str) -> bool:
        if self.password_hash is None:
            return True
        given = password.encode()
        with self._lock:
            # A password already verified once is compared directly, so a
            # script polling with Bearer does not pay scrypt every time.
            if self._known is not None and hmac.compare_digest(given, self._known):
                return True
            if verify_password(password, self.password_hash):
                self._known = given
                return True
            return False


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


def _host(request: Request) -> str:
    return (urlsplit(f"//{request.headers.get('host', '')}").hostname or "").lower()


def _same_origin(request: Request, origin: str) -> bool:
    own = f"{request.url.scheme}://{request.headers.get('host', '')}"
    return origin.lower() == own.lower()


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=403, detail=detail)


def _unauthorized() -> HTTPException:
    # No WWW-Authenticate: Basic, which would make a browser prompt.
    return HTTPException(status_code=401, detail="sign in, or send Authorization: Bearer")


def _check_origin(request: Request, *, by_cookie: bool) -> None:
    if request.method in SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is None:
        if by_cookie:
            raise _forbidden("a signed-in request that changes state must carry an Origin header")
        return
    if not _same_origin(request, origin):
        raise _forbidden(f"cross-site request from {origin} refused")


def authorize(request: Request) -> None:
    """The dependency every API route and the API docs sit behind."""
    gate: Gate = request.app.state.gate
    if gate.open:
        _check_origin(request, by_cookie=False)
        return
    token = _bearer(request)
    if token is not None:
        # A script: the password itself, no ambient credential, so no
        # Origin rule is needed.
        if not gate.check(token):
            raise _unauthorized()
        return
    if not request.session.get("signed_in"):
        raise _unauthorized()
    _check_origin(request, by_cookie=True)


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
    value: float = Field(allow_inf_nan=False)


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
    devices: list[DeviceOut]


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
    return StatusOut(version=version("openreactor"), run=svc.current_run(), devices=devices)


@api.get("/channels")
def get_channels(svc: Svc) -> list[ChannelOut]:
    return [ChannelOut(**asdict(c)) for c in svc.channels()]


@api.put("/channels/{channel}/setpoint", status_code=204)
def put_setpoint(channel: str, body: Setpoint, svc: Svc) -> None:
    svc.set_setpoint(channel, body.value)


@api.post("/stop-all")
def post_stop_all(svc: Svc) -> list[EventOut]:
    return [EventOut(**asdict(e)) for e in svc.stop_all()]


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
def get_export(run: int, svc: Svc) -> Response:
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
    """``start`` opens the circuits and starts the controller, and on exit
    sends stop-all and closes them (``service.running``)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with start() as svc:
            app.state.service = svc
            yield

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
    # A new secret each start: a restart signs everyone out.
    app.add_middleware(
        SessionMiddleware,
        secret_key=secrets.token_urlsafe(32),
        session_cookie=SESSION_COOKIE,
        max_age=SESSION_MAX_AGE_S,
        same_site="strict",
    )

    @app.middleware("http")
    async def loopback_hosts_only(request: Request, call_next: Any) -> Any:
        # Without a password the API trusts whoever reaches it, which only
        # holds under a loopback Host name.
        if app.state.gate.open and _host(request) not in LOOPBACK_HOSTS:
            return JSONResponse({"detail": "unknown Host"}, status_code=403)
        return await call_next(request)

    app.include_router(api)

    @app.get("/api/v1/openapi.json", include_in_schema=False, dependencies=[Depends(authorize)])
    def openapi() -> dict[str, Any]:
        return app.openapi()

    @app.post("/login", status_code=204, include_in_schema=False)
    async def login(request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin is not None and not _same_origin(request, origin):
            raise _forbidden(f"cross-site request from {origin} refused")
        password = await _password_from(request)
        # scrypt is slow on purpose; keep it off the event loop.
        if not await run_in_threadpool(app.state.gate.check, password):
            raise HTTPException(status_code=401, detail="wrong password")
        request.session.clear()
        request.session["signed_in"] = True
        return Response(status_code=204)

    @app.post("/logout", status_code=204, include_in_schema=False)
    def logout(request: Request) -> Response:
        request.session.clear()
        return Response(status_code=204)

    for error, code in (
        (NotFound, 404),
        (Conflict, 409),
        (Unavailable, 409),
        (Invalid, 422),
        (ControllerClosed, 503),
        (FutureTimeout, 504),
    ):

        def handler(request: Request, exc: Exception, code: int = code) -> JSONResponse:
            detail = str(exc) or "the controller did not finish in time"
            return JSONResponse({"detail": detail}, status_code=code)

        app.add_exception_handler(error, handler)

    return app
