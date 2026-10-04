"""The operator's browser UI: server-rendered pages with htmx partials.

Every page and action goes through the same service layer as /api/v1, and
sits behind the same sign-in. Pages redirect to /login; actions use the
API's own check, Origin rule included. Nothing is loaded from the internet:
Bootstrap, htmx and Chart.js are vendored under static/vendor.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from openreactor.config import SETPOINT_MAX_C, SLICE_KINDS
from openreactor.ezo import FAMILIES
from openreactor.service import Service

HERE = Path(__file__).parent
SOURCE_URL = "https://github.com/uwo-fast/openreactor"

templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["utc"] = lambda t: datetime.fromtimestamp(t, UTC).strftime(
    "%Y-%m-%d %H:%M:%S"
)


class SignInFirst(Exception):
    """A page asked for without a session: answered with a redirect."""


def page_access(request: Request) -> None:
    gate = request.app.state.gate
    if not gate.open and not gate.signed_in(request.session.get("id")):
        raise SignInFirst


def _authorize(request: Request) -> None:
    from openreactor.web import authorize

    authorize(request)


def _service(request: Request) -> Service:
    return request.app.state.service


pages = APIRouter(include_in_schema=False, dependencies=[Depends(page_access)])
actions = APIRouter(prefix="/ui", include_in_schema=False, dependencies=[Depends(_authorize)])


def _vendored() -> list[dict[str, str]]:
    manifest = HERE / "static" / "vendor" / "vendor.toml"
    seen: dict[str, dict[str, str]] = {}
    for f in tomllib.loads(manifest.read_text())["file"]:
        seen.setdefault(f["package"], f)
    return list(seen.values())


def _render(request: Request, name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    gate = request.app.state.gate
    context.update(
        version=version("openreactor"),
        source_url=SOURCE_URL,
        signed_in=not gate.open and gate.signed_in(request.session.get("id")),
        path=request.url.path,
    )
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def login_page(request: Request, error: str = "", status_code: int = 200) -> HTMLResponse:
    return _render(request, "login.html", status_code=status_code, error=error)


def _actuated(svc: Service) -> list[dict[str, Any]]:
    return [
        {
            "name": ch.name,
            "label": ch.label,
            "device": d.name,
            "kind": d.kind,
            "max": ch.max_setpoint if ch.max_setpoint is not None else SETPOINT_MAX_C,
        }
        for d in svc.config.devices
        if d.kind in SLICE_KINDS
        for ch in d.channels
    ]


def _ezo_devices(svc: Service) -> list[dict[str, Any]]:
    out = []
    for d in svc.config.devices:
        family = FAMILIES.get(d.kind)
        if family is not None:
            points = [{"name": p, "needs_value": needs} for p, needs in family.points.items()]
            out.append({"name": d.name, "kind": d.kind, "points": points})
    return out


# Pages


@pages.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    svc = _service(request)
    return _render(
        request,
        "dashboard.html",
        run=svc.current_run(),
        recording_failed=svc.recording_failed(),
        devices=svc.devices(),
    )


@pages.get("/controls", response_class=HTMLResponse)
def controls(request: Request) -> HTMLResponse:
    return _render(request, "controls.html", channels=_actuated(_service(request)))


@pages.get("/runs", response_class=HTMLResponse)
def runs(request: Request) -> HTMLResponse:
    return _render(request, "runs.html")


@pages.get("/devices", response_class=HTMLResponse)
def devices(request: Request) -> HTMLResponse:
    svc = _service(request)
    return _render(request, "devices.html", devices=svc.devices(), ezo=_ezo_devices(svc))


@pages.get("/about", response_class=HTMLResponse)
def about(request: Request) -> HTMLResponse:
    return _render(request, "about.html", vendored=_vendored())


# Partials and actions: htmx swaps the HTML they return into the page.


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8", "replace")
    return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}


def _notice(request: Request, message: str, *, ok: bool, status_code: int = 200) -> HTMLResponse:
    return _render(request, "_notice.html", status_code=status_code, message=message, ok=ok)


def _errors() -> tuple[tuple[type[Exception], int], ...]:
    from openreactor.web import ERRORS

    return ERRORS


def _error_notice(request: Request, exc: Exception) -> HTMLResponse:
    """The error as the API would report it, with the API's status."""
    from openreactor.web import error_detail

    code = next(c for e, c in _errors() if isinstance(exc, e))
    return _notice(request, error_detail(exc), ok=False, status_code=code)


async def _act(request: Request, work: Callable[[], str], *, changes: bool = True) -> HTMLResponse:
    """Run ``work`` off the event loop: its message, or its error."""
    try:
        message = await run_in_threadpool(work)
    except tuple(e for e, _ in _errors()) as exc:
        return _error_notice(request, exc)
    response = _notice(request, message, ok=True)
    if changes:
        # Lets the parts of the page that show runs or calibration refresh.
        response.headers["HX-Trigger"] = "changed"
    return response


@actions.get("/channels", response_class=HTMLResponse)
def channels_partial(request: Request) -> HTMLResponse:
    return _render(request, "_channels.html", channels=_service(request).channels())


@actions.get("/runs", response_class=HTMLResponse)
def runs_partial(request: Request) -> HTMLResponse:
    svc = _service(request)
    return _render(
        request,
        "_runs.html",
        run=svc.current_run(),
        recording_failed=svc.recording_failed(),
        runs=list(reversed(svc.runs())),
    )


@actions.post("/stop-all", response_class=HTMLResponse)
async def stop_all(request: Request) -> HTMLResponse:
    svc = _service(request)

    try:
        events = await run_in_threadpool(svc.stop_all)
    except tuple(e for e, _ in _errors()) as exc:
        return _error_notice(request, exc)
    failed = [e for e in events if e.result != "ok"]
    if failed:
        # Said plainly: an output that did not go safe is the one thing the
        # operator must not miss.
        detail = "; ".join(f"{e.device or 'stop-all'}: {e.result}" for e in failed)
        return _notice(request, f"Stop-all FAILED for {detail}", ok=False, status_code=502)
    sent = ", ".join(e.device for e in events if e.device)
    return _notice(request, f"Stop-all sent: {sent or events[0].details}.", ok=True)


@actions.post("/runs", response_class=HTMLResponse)
async def start_run(request: Request) -> HTMLResponse:
    form = await _form(request)
    name, notes = form.get("name", "").strip(), form.get("notes", "")
    if not name:
        return _notice(request, "A run needs a name.", ok=False, status_code=422)
    svc = _service(request)
    return await _act(request, lambda: f"Run {svc.start_run(name, notes)} is recording.")


@actions.post("/runs/stop", response_class=HTMLResponse)
async def stop_run(request: Request) -> HTMLResponse:
    svc = _service(request)

    def work() -> str:
        run, status = svc.stop_run()
        return f"Run {run} {status}."

    return await _act(request, work)


@actions.post("/channels/{channel}/setpoint", response_class=HTMLResponse)
async def setpoint(request: Request, channel: str) -> HTMLResponse:
    form = await _form(request)
    try:
        value = float(form.get("value", ""))
    except ValueError:
        return _notice(request, "The setpoint must be a number.", ok=False, status_code=422)
    svc = _service(request)

    def work() -> str:
        svc.set_setpoint(channel, value)
        return f"{channel} set to {round(value * 10) / 10:g} °C."

    return await _act(request, work)


@actions.get("/ezo/{device}/calibration", response_class=HTMLResponse)
async def calibration(request: Request, device: str) -> HTMLResponse:
    svc = _service(request)
    return await _act(request, lambda: f"{device}: {svc.calibration(device)}", changes=False)


@actions.post("/ezo/{device}/calibration", response_class=HTMLResponse)
async def calibrate(request: Request, device: str) -> HTMLResponse:
    form = await _form(request)
    point, raw = form.get("point", ""), form.get("value", "").strip()
    try:
        value = float(raw) if raw else None
    except ValueError:
        return _notice(request, "The reference value must be a number.", ok=False, status_code=422)
    svc = _service(request)
    return await _act(request, lambda: f"{device}: {svc.calibrate(device, point, value)}")


@actions.post("/ezo/{device}/calibration/clear", response_class=HTMLResponse)
async def clear_calibration(request: Request, device: str) -> HTMLResponse:
    svc = _service(request)
    return await _act(request, lambda: f"{device}: {svc.clear_calibration(device)}")


class NoFraming:
    """Forbid other sites from framing any page: without a password, a page
    framed on another site could otherwise be clicked through."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def framed_send(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-frame-options", b"DENY"))
                headers.append((b"content-security-policy", b"frame-ancestors 'none'"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, framed_send)


def install(app: FastAPI) -> None:
    app.add_middleware(NoFraming)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(pages)
    app.include_router(actions)

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def get_login(request: Request) -> Response:
        if request.app.state.gate.open:
            return RedirectResponse("/", status_code=303)
        return login_page(request)

    @app.exception_handler(SignInFirst)
    def sign_in_first(request: Request, exc: SignInFirst) -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        # htmx swaps the response into the page: give it a notice, not JSON.
        if "hx-request" in request.headers:
            response = _notice(request, str(exc.detail), ok=False, status_code=exc.status_code)
            if exc.status_code == 401:
                # The session ended (sign-out, 12 hours, a restart): take the
                # whole page to sign in rather than fail every poll.
                response.headers["HX-Redirect"] = "/login"
            return response
        from fastapi.exception_handlers import http_exception_handler

        return await http_exception_handler(request, exc)
