"""The loopback boundary of the unauthenticated board API.

The board answers on ``127.0.0.1:8420`` with no authentication, and a loopback
address is not an authentication boundary: any page the operator visits while
the board is up can reach it from the browser. The same-user trust boundary
docs/security.md draws ("an attacker with shell as the nh user" is out of
scope) is enforced here as four checks, in one place:

* :func:`require_local_host` (middleware) — every request must carry a loopback
  ``Host``, else 400. This is the DNS-rebinding defence: a rebound request is
  same-origin to the browser and sends no ``Origin`` at all, so ``Host`` is the
  only field that still names the attacker's domain. It also means a board
  bound to a non-loopback interface (``nh start --host 0.0.0.0``) refuses
  requests addressed by a LAN name or IP — by design, documented in
  docs/security.md.
* :func:`refuse_cross_origin_writes` (middleware) — a state-changing request
  whose ``Origin`` is present and not loopback is refused (403). A browser
  always sends ``Origin`` on a cross-site write; an absent ``Origin`` is a
  non-browser local client (the ``nh`` CLI, the MCP bridge) and is allowed.
  Only the mutating verbs are checked, so the CORS preflight and every read
  pass through.
* :data:`LOOPBACK_ORIGIN_REGEX` — the CORS grant, exact-host loopback on any
  port, so a cross-origin page cannot read responses. Exact, not
  ``startswith``: ``http://localhost.evil.com`` is a domain an attacker
  registers (that bug shipped once, see :func:`require_local_origin`).
* :func:`ws_handshake_is_local` — the same ``Host``/``Origin`` gate for the
  WebSocket handshake, which bypasses CORS and the HTTP middlewares.

:func:`require_local_origin` is the older per-route check the credential
routes call explicitly; it additionally refuses a WRITE with no ``Origin`` at
all, which the global middleware deliberately does not (the CLI has none).
"""
from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
LOOPBACK_ORIGIN_REGEX = r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$"
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def host_is_local(host_header: str | None) -> bool:
    """Whether a ``Host`` header value names a loopback host (any port)."""
    return (urlsplit("//" + (host_header or "")).hostname or "") in LOCAL_HOSTS


def origin_is_local(origin: str | None) -> bool:
    """Whether an ``Origin`` header value is an http(s) loopback origin. An
    absent origin is NOT local — callers decide what absence means."""
    if origin is None:
        return False
    parts = urlsplit(origin)
    return parts.scheme in ("http", "https") and (parts.hostname or "") in LOCAL_HOSTS


def ws_handshake_is_local(headers: Mapping[str, str]) -> bool:
    """The WebSocket handshake gate: loopback ``Host``, and ``Origin`` either
    absent (a non-browser client) or loopback."""
    origin = headers.get("origin")
    return host_is_local(headers.get("host")) and (origin is None or origin_is_local(origin))


async def require_local_host(request: Request, call_next):
    """Refuse a request whose ``Host`` header is not loopback (400)."""
    if not host_is_local(request.headers.get("host")):
        return JSONResponse(
            status_code=400,
            content={
                "error": "bad_host",
                "reason": (
                    "requests must address the board on a loopback host "
                    "(see docs/security.md)."
                ),
            },
        )
    return await call_next(request)


async def refuse_cross_origin_writes(request: Request, call_next):
    """Refuse a cross-origin browser write to any state-changing route (403)."""
    if request.method.upper() in _WRITE_METHODS:
        origin = request.headers.get("origin")
        if origin is not None and not origin_is_local(origin):
            return JSONResponse(
                status_code=403,
                content={
                    "error": "cross_origin_refused",
                    "reason": (
                        "cross-origin writes are not allowed to the local "
                        "board API (see docs/security.md)."
                    ),
                },
            )
    return await call_next(request)


def install_local_boundary(app) -> None:
    """Register the two boundary middlewares. Each ``app.middleware`` call
    wraps OUTSIDE the ones before it, so the origin check runs first, then the
    host check, then whatever the app registered earlier."""
    app.middleware("http")(require_local_host)
    app.middleware("http")(refuse_cross_origin_writes)


def require_local_origin(request: Request, *, writing: bool = False) -> None:
    """Refuse a cross-origin call to the credential routes.

    The server is unauthenticated, so without this ANY page the operator
    visits while `nh serve` is up could PUT the token endpoint and replace the
    token that pays for the subscription. The global middlewares above now
    cover every write; this per-route check remains for the credential routes
    because it is stricter on one point:

    On a WRITE, a missing ``Origin`` is refused too. A browser always sends it
    on a cross-site request, so the legitimate Settings UI is unaffected, and
    it is the one case where a local malicious process or a rebinding proxy
    would otherwise face no check at all.

    The host is compared EXACTLY, after parsing. A ``startswith`` prefix test
    looks equivalent and is not: ``http://localhost.evil.com`` starts with
    ``http://localhost``, and that is a domain an attacker registers. That
    exact bug shipped here and was caught with a working drive-by.
    """
    origin = request.headers.get("origin")
    if origin is None:
        if writing:
            raise HTTPException(
                403, "this endpoint requires a same-origin browser request")
        return
    if not origin_is_local(origin):
        raise HTTPException(403, "cross-origin requests are not allowed here")
