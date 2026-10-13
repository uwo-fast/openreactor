"""Decide who may reach the API and the UI, and guard every request.

This holds the request checks that the API and the UI share: the sign-in
and Origin rules, and the middleware that limits the body size, the Host
and framing. The password check itself, ``Gate``, lives in ``web``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
SESSION_COOKIE = "openreactor_session"
SESSION_MAX_AGE_S = 12 * 60 * 60
# No request body here is more than a few hundred bytes; anything far
# larger is refused before it is read, signed in or not.
MAX_BODY_BYTES = 64 * 1024


class LoopbackHostsOnly:
    """Refuse any request whose Host is not a loopback name.

    Without a password the API trusts whoever reaches it, which only holds
    under a loopback Host. A page using DNS rebinding sends another.
    """

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            host = dict(scope["headers"]).get(b"host", b"").decode("latin-1")
            try:
                name = (urlsplit(f"//{host}").hostname or "").lower()
            except ValueError:  # such as "[::1" with no closing bracket
                name = ""
            if name not in LOOPBACK_HOSTS:
                await JSONResponse({"detail": "unknown Host"}, status_code=403)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


class TooLarge(HTTPException):
    """Signal from receive() that the request body is over the limit.

    As an HTTPException, FastAPI's body parsing passes it on as a 413
    instead of turning it into a 400.
    """

    def __init__(self) -> None:
        super().__init__(status_code=413, detail="request body too large")


def _too_large(e: BaseException) -> bool:
    if isinstance(e, TooLarge):
        return True
    return isinstance(e, BaseExceptionGroup) and any(_too_large(x) for x in e.exceptions)


class BodyLimit:
    """Refuse a request body over ``limit`` bytes with 413.

    It checks the Content-Length before reading anything, then counts the
    body as it arrives.
    """

    def __init__(self, app: Any, limit: int = MAX_BODY_BYTES):
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope["headers"]:
            if name == b"content-length" and (not value.isdigit() or int(value) > self.limit):
                await self._refuse(send)
                return
        received = 0
        started = False

        async def counted_receive() -> Any:
            # A body sent without a Content-Length is counted as it comes.
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise TooLarge
            return message

        async def tracked_send(message: Any) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counted_receive, tracked_send)
        except BaseException as e:
            # Answer here unless a route already turned it into its own 413.
            if not _too_large(e) or started:
                raise
            await self._refuse(send)

    @staticmethod
    async def _refuse(send: Any) -> None:
        body = b'{"detail":"request body too large"}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class NoFraming:
    """Forbid other sites from framing any page.

    Without a password, a page framed on another site could otherwise be
    clicked through.
    """

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


class Busy(Exception):
    """Another password check is running."""


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


def same_origin(request: Request, origin: str) -> bool:
    own = f"{request.url.scheme}://{request.headers.get('host', '')}"
    return origin.lower() == own.lower()


def forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=403, detail=detail)


def client(request: Request) -> str:
    return request.client.host if request.client else ""


def busy() -> HTTPException:
    return HTTPException(status_code=429, detail="another password check is running; try again")


def _unauthorized() -> HTTPException:
    # No WWW-Authenticate: Basic, which would make a browser prompt.
    return HTTPException(status_code=401, detail="sign in, or send Authorization: Bearer")


def check_origin(request: Request, *, by_cookie: bool) -> None:
    if request.method in SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is None:
        if by_cookie:
            raise forbidden("a signed-in request that changes state must carry an Origin header")
        return
    if not same_origin(request, origin):
        raise forbidden(f"cross-site request from {origin} refused")


def authorize(request: Request) -> None:
    """Raise unless the request may use the API.

    Every API route, every UI action and the API docs depend on this.
    """
    gate = request.app.state.gate
    if gate.open:
        check_origin(request, by_cookie=False)
        return
    token = _bearer(request)
    if token is not None:
        # A script sends the password itself, not an ambient credential a
        # browser adds, so no Origin rule is needed.
        try:
            ok = gate.check(token, client(request))
        except Busy:
            raise busy() from None
        if not ok:
            raise _unauthorized()
        return
    if not gate.signed_in(request.session.get("id")):
        raise _unauthorized()
    check_origin(request, by_cookie=True)
