"""The loopback boundary of the unauthenticated board API.

The board answers on ``127.0.0.1:8420`` with no authentication, and a loopback
address is not an authentication boundary: any page the operator visits while
the board is up can reach it from the browser. The same-user trust boundary
docs/security.md draws ("an attacker with shell as the nh user" is out of
scope) is enforced here as four checks, in one place:

* ``Host`` must name an allowed host, else 400. This is the DNS-rebinding
  defence: a rebound request is same-origin to the browser and sends no
  ``Origin`` at all, so ``Host`` is the only field that still names the
  attacker's domain. Allowed = loopback, plus the host the server was
  configured to bind (``server.host``): the ``nh`` CLI and ``nh start`` address
  the board by that configured value (``cli/shell.py:base_url_from_config``),
  and a rebinding page cannot make its own domain equal it.
* A state-changing request whose ``Origin`` is present and not allowed is
  refused (403). A browser always sends ``Origin`` on a cross-site write; an
  absent ``Origin`` is a non-browser local client (the ``nh`` CLI, the MCP
  bridge) and is allowed. Only the mutating verbs are checked, so the CORS
  preflight and every read pass through.
* :data:`LOOPBACK_ORIGIN_REGEX` — the CORS grant, exact-host loopback on any
  port, so a cross-origin page cannot read responses. Exact, not
  ``startswith``: ``http://localhost.evil.com`` is a domain an attacker
  registers (that bug shipped once, see :func:`require_local_origin`). The
  board page itself is same-origin and needs no CORS grant, so a configured
  non-loopback ``server.host`` is deliberately NOT in the grant.
* :func:`ws_handshake_is_local` — the same ``Host``/``Origin`` gate for the
  WebSocket handshake, which bypasses CORS and the HTTP middlewares.

The two HTTP checks run in ONE middleware dispatch (:func:`local_boundary`):
each ``BaseHTTPMiddleware`` layer costs on the order of 100 µs per request on
a board the UI polls, and both checks are header predicates.

:func:`require_local_origin` is the older per-route check the credential
routes call explicitly; it additionally refuses a WRITE with no ``Origin`` at
all, which the global middleware deliberately does not (the CLI has none).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
LOOPBACK_ORIGIN_REGEX = r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$"
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _hostname(authority_or_url: str) -> str:
    """The lower-cased hostname of a ``Host`` value or a URL, or ``""``.

    Userinfo is refused outright: a browser never sends it in either header,
    so ``evil.com@127.0.0.1`` is a hand-built request, and parsing it would
    let ``urlsplit`` report the part after the ``@`` as the host."""
    if "@" in authority_or_url:
        return ""
    value = authority_or_url if "//" in authority_or_url else "//" + authority_or_url
    try:
        return (urlsplit(value).hostname or "").lower()
    except ValueError:  # e.g. an unbracketed IPv6 literal with a port
        return ""


def allowed_hosts(app) -> frozenset[str]:
    """Loopback plus the configured bind host (``server.host``), if any."""
    cfg = getattr(getattr(app, "state", None), "config", None)
    data = getattr(cfg, "data", cfg)
    server = data.get("server") if isinstance(data, dict) else None
    host = (server or {}).get("host") if isinstance(server, dict) else None
    if isinstance(host, str) and host.strip():
        return LOCAL_HOSTS | {host.strip().strip("[]").lower()}
    return LOCAL_HOSTS


def host_is_local(host_header: str | None, allowed: Iterable[str] = LOCAL_HOSTS) -> bool:
    """Whether a ``Host`` header value names an allowed host (any port)."""
    return _hostname(host_header or "") in allowed


def origin_is_local(origin: str | None, allowed: Iterable[str] = LOCAL_HOSTS) -> bool:
    """Whether an ``Origin`` header value is an http(s) origin on an allowed
    host. An absent origin is NOT local — callers decide what absence means."""
    if origin is None:
        return False
    parts = urlsplit(origin)
    return parts.scheme in ("http", "https") and _hostname(origin) in allowed


def ws_handshake_is_local(headers: Mapping[str, str],
                          allowed: Iterable[str] = LOCAL_HOSTS) -> bool:
    """The WebSocket handshake gate: allowed ``Host``, and ``Origin`` either
    absent (a non-browser client) or allowed."""
    origin = headers.get("origin")
    return host_is_local(headers.get("host"), allowed) and (
        origin is None or origin_is_local(origin, allowed))


# Content-Security-Policy for the board HTML document (applied to text/html
# responses in the middleware below). The built index.html loads one same-origin
# module and no inline script, so `script-src 'self'` is strict and
# non-breaking: an injected <script> or inline handler cannot execute even if
# markup slipped past react-markdown's escaping. Board XSS is same-origin and
# would otherwise reach the whole API; this closes the script-execution half.
# Inline STYLE is allowed (React style props / bundler CSS); object-src 'none',
# base-uri 'self', frame-ancestors 'none' and form-action 'self' close the
# plugin, base-tag, clickjacking and form-redirect vectors.
_BOARD_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)
# Trusted Types is sent REPORT-ONLY: the board's own code uses no guarded DOM
# sink, but bundled dependencies call innerHTML, so enforcing this could break
# rendering until each is confirmed TrustedHTML-safe in a real browser.
_BOARD_CSP_REPORT_ONLY = "require-trusted-types-for 'script'"


async def local_boundary(request: Request, call_next):
    """One dispatch, two refusals: a cross-origin browser write (403), then a
    request whose ``Host`` is not an allowed host (400)."""
    allowed = allowed_hosts(request.app)
    if request.method.upper() in _WRITE_METHODS:
        origin = request.headers.get("origin")
        if origin is not None and not origin_is_local(origin, allowed):
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
    if not host_is_local(request.headers.get("host"), allowed):
        return JSONResponse(
            status_code=400,
            content={
                "error": "bad_host",
                "reason": (
                    "requests must address the board on a loopback host or "
                    "its configured server.host (see docs/security.md)."
                ),
            },
        )
    response = await call_next(request)
    # CSP is defense-in-depth for the board document; applied to HTML only.
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers.setdefault("Content-Security-Policy", _BOARD_CSP)
        response.headers.setdefault(
            "Content-Security-Policy-Report-Only", _BOARD_CSP_REPORT_ONLY)
    return response


def install_local_boundary(app) -> None:
    """Register the boundary as the outermost HTTP middleware (each
    ``app.middleware`` call wraps OUTSIDE the ones before it)."""
    app.middleware("http")(local_boundary)


def require_local_origin(request: Request, *, writing: bool = False) -> None:
    """Refuse a cross-origin call to the credential routes.

    The server is unauthenticated, so without this ANY page the operator
    visits while `nh serve` is up could PUT the token endpoint and replace the
    token that pays for the subscription. The global middleware above now
    covers every write; this per-route check remains for the credential routes
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
    if not origin_is_local(origin, allowed_hosts(request.app)):
        raise HTTPException(403, "cross-origin requests are not allowed here")
