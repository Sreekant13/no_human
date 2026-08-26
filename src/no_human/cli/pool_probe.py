"""What `nh status` may claim about the worker pool, in one place.

`nh status` prints a `working N/M` ratio. When `/api/queue/health` answers,
M is observed. When it does not, M comes from config and the line carries a
note saying so — and that note used to be "server not running" for EVERY way
the probe could fail. A probe that merely stalled past its timeout therefore
printed a definitive claim about a server that was up and answering.

This module holds the classifier and the words. The rule it exists to enforce:
**the note may only say what the probe established.** A connection refused on
the configured host/port really is evidence there is no listener. A timeout, a
DNS failure, a protocol error, an HTTP 500 and an unreadable body are five
different, weaker facts — and the last two are cases where the server
demonstrably ANSWERED, so "not running" is not merely unproven there, it is
contradicted by the probe's own evidence.

The classifier also fails toward the claim that asserts least: a failure nobody
enumerated lands on `POOL_UNREACHABLE` (see `_pool_note`'s `.get` default),
never back on `POOL_REFUSED`. `POOL_UNREACHABLE`'s note therefore describes no
mechanism at all — only that no readable answer came back and the pool state
is unknown. It cannot: the outcome covers a connection that never opened AND an
`IncompleteRead` after a 200, where the server plainly did answer.
It is a probe for a status line, so it must also never raise:
`_probe_pool` returning "I could not tell" is a worse status line, while an
exception is no status line at all.

`nh status` is the only consumer of the outcome; `commands._running_pool_stats`
keeps the older stats-only contract for callers (`task_show`) that just want
the numbers.
"""

from __future__ import annotations

from typing import NamedTuple

#: How long the probe waits for `/api/queue/health`, in seconds. ONE source of
#: truth: `_probe_pool` passes it to `urlopen` and `_POOL_NOTES` formats the
#: timeout note from it, so the number an operator reads is the number that
#: was actually waited. It used to be a bare `1.5` in each place, and moving
#: one left the other lying.
PROBE_TIMEOUT_S = 1.5

#: What the pool probe ESTABLISHED, as distinct from what it wanted to learn.
POOL_LIVE = "live"                  # 200 + a scheduler width >= 1: stats are real
POOL_REFUSED = "refused"            # connection refused on the configured host/port
POOL_NO_SCHEDULER = "no_scheduler"  # answered 200, reported no pool (width < 1)
POOL_TIMEOUT = "timeout"            # no answer inside PROBE_TIMEOUT_S
POOL_UNREACHABLE = "unreachable"    # no readable answer came back; why is unknown
POOL_HTTP_ERROR = "http_error"      # it ANSWERED, with a status other than 200
POOL_BAD_BODY = "bad_body"          # it answered 200; the body was unusable


class PoolProbe(NamedTuple):
    stats: tuple[int | None, int, dict | None] | None
    outcome: str
    #: The status code seen, for `POOL_HTTP_ERROR` only — the note names it.
    #: None for every other outcome, and also for an answer that carried no
    #: readable status at all.
    http_status: int | None = None


#: `nh status`'s note for each outcome that carries no live stats — kept as a
#: mapping (rather than inline in `status`) so the outcome->note relation is
#: driven directly by `test_pool_note_says_only_what_that_outcome_established`
#: instead of being scraped out of the console line.
#:
#: Only `POOL_REFUSED` says "server not running". `POOL_HTTP_ERROR` and
#: `POOL_BAD_BODY` say the server answered, because it did. `POOL_UNREACHABLE`
#: names no mechanism (it covers causes where the server did answer and causes
#: where it never did), and it and `POOL_TIMEOUT` carry the caveat that the
#: printed width is the configured one and may be wrong.
_POOL_NOTES = {
    POOL_REFUSED: " [dim](configured; server not running)[/]",
    POOL_TIMEOUT: (
        f" [dim](configured; pool unreachable — no answer in {PROBE_TIMEOUT_S}s, "
        "this width may be wrong)[/]"
    ),
    POOL_UNREACHABLE: (
        " [dim](configured; could not get a readable answer from the server "
        "— pool state unknown, this width may be wrong)[/]"
    ),
    POOL_HTTP_ERROR: (
        " [dim](configured; server answered HTTP {code} — pool state unknown)[/]"
    ),
    POOL_BAD_BODY: (
        " [dim](configured; server answered but the response was unreadable "
        "— pool state unknown)[/]"
    ),
    POOL_NO_SCHEDULER: " [dim](configured; server up, no pool attached)[/]",
}


def _pool_note(outcome: str, http_status: int | None = None) -> str:
    """The `nh status` note for a `PoolProbe.outcome` that carries no stats.

    `POOL_REFUSED` is the only outcome that establishes "no listener". An
    outcome with no note of its own degrades to `POOL_UNREACHABLE`'s, which
    claims nothing beyond "pool state unknown" — no listener state, no
    mechanism — so a new outcome added without a note can neither resurrect
    "server not running" for a server that is up nor describe a failure whose
    shape nobody here knows.
    """
    if outcome == POOL_HTTP_ERROR:
        code = "an unreadable status" if http_status is None else http_status
        return _POOL_NOTES[POOL_HTTP_ERROR].format(code=code)
    return _POOL_NOTES.get(outcome, _POOL_NOTES[POOL_UNREACHABLE])


def _probe_pool(config) -> PoolProbe:
    """Probe `/api/queue/health` and classify what was actually established.

    Never raises: every failure becomes an outcome, because the caller is a
    status line and "I could not tell" beats a traceback. The except tuple
    below is not exhaustive by inspection — it is exhaustive by construction
    only for `urllib`/`socket`/`http.client` failures, so anything outside it
    would still escape; `http.client.HTTPException` is in the tuple because
    `BadStatusLine`/`InvalidURL`/`IncompleteRead` are NOT `OSError` or
    `URLError` subclasses and used to crash `nh status` outright.

    See `commands._running_pool_stats` for the stats contract this wraps.
    """
    import http.client
    import json as _json
    import socket
    import urllib.error
    import urllib.request

    srv = config.get("server", {}) or {}
    host = srv.get("host", "127.0.0.1")
    port = srv.get("port", 8420)
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/api/queue/health", timeout=PROBE_TIMEOUT_S
        ) as resp:
            status = getattr(resp, "status", None)
            if status != 200:
                # 2xx is what `urlopen` RETURNS (see the handler below for
                # what happens to the rest), so this is the 204/206 shape: it
                # ANSWERED, just not with a pool report.
                return PoolProbe(None, POOL_HTTP_ERROR,
                                 status if isinstance(status, int) else None)
            body = resp.read()
    except (urllib.error.URLError, OSError, http.client.HTTPException,
            ValueError, TypeError, TimeoutError, socket.timeout) as exc:
        # AN ANSWERED 4xx/5xx ARRIVES HERE, NOT ABOVE. `urlopen` does not
        # RETURN those: `urllib.request.HTTPErrorProcessor.http_response`
        # routes every non-2xx into the opener's error path, where
        # `HTTPDefaultErrorHandler` raises `HTTPError` for anything no handler
        # claims (`HTTPRedirectHandler` claims 301/302/303/307/308 and follows
        # them; nothing claims 4xx/5xx). `HTTPError` is a `URLError` is an
        # `OSError`, so a real 500/503/404 lands in this except tuple, among
        # the connection failures. Unless it is named here it falls through to
        # the `POOL_UNREACHABLE` return at the bottom, reporting "no readable
        # answer" for a server that answered with a code it read fine.
        if isinstance(exc, urllib.error.HTTPError):
            code = getattr(exc, "status", None)
            if not isinstance(code, int):
                code = getattr(exc, "code", None)
            return PoolProbe(None, POOL_HTTP_ERROR,
                             code if isinstance(code, int) else None)
        # `urlopen` on a closed local port raises `URLError(reason=
        # ConnectionRefusedError(...))` — the refused case is only visible
        # through `.reason`, not on the wrapper itself. Same for a timeout.
        inner = getattr(exc, "reason", exc)
        for candidate in (exc, inner):
            if isinstance(candidate, ConnectionRefusedError):
                return PoolProbe(None, POOL_REFUSED)
            if isinstance(candidate, (TimeoutError, socket.timeout)):
                return PoolProbe(None, POOL_TIMEOUT)
        return PoolProbe(None, POOL_UNREACHABLE)

    try:
        payload = _json.loads(body or b"{}")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return PoolProbe(None, POOL_BAD_BODY)
        width = int(payload.get("max_workers", 0))
    except (ValueError, TypeError):
        return PoolProbe(None, POOL_BAD_BODY)

    if width < 1:
        return PoolProbe(None, POOL_NO_SCHEDULER)
    busy_raw = payload.get("workers_busy")
    try:
        busy = int(busy_raw) if busy_raw is not None else None
    except (ValueError, TypeError):
        # A numerator we cannot read is not a numerator, but it does not
        # discredit a width that WAS read: dropping the field to None (the
        # same value an ABSENT `workers_busy` produces — see
        # `_running_pool_stats`) keeps the observed denominator and makes the
        # caller count rows for the numerator, where POOL_BAD_BODY would
        # discard the width for the configured guess. It also has to be caught
        # here rather than left to the body-parsing `try` above: this function
        # may not raise, and `int("many")` does.
        busy = None
    if busy is not None and busy < 0:
        # `workers_busy` is `len(inflight_ids)`; a negative one parses but
        # cannot be true, so it is unreadable in the same sense.
        busy = None
    pause = None
    if payload.get("paused"):
        pause = {
            "reason": payload.get("paused_reason"),
            "until": payload.get("paused_until"),
            "profile": payload.get("paused_profile"),
        }
    return PoolProbe((busy, width, pause), POOL_LIVE)
