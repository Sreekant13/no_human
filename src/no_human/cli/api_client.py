"""HTTP client for the conversational shell (`nh` with no arguments).

The shell talks to the running server over http://127.0.0.1:8420 and never
opens the SQLite file. `nh watch`'s TUI does the opposite — it constructs
`Store(config.db_path)` while `nh start` holds the same file — and pays for it
with the known sqlite race. Going over the API also means the shell can point
at a remote instance later by changing one URL.

Errors are values, not stack traces: a refused connection raises
:class:`NhServerUnreachable`, and any non-2xx raises :class:`NhApiError`
carrying the server's own ``detail`` string.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8420"

# The grill stream can sit silent while the subagent greps a repo; the server
# itself gives up at 130s (app.py `_generate`). Task event streams are open
# for the life of a run, so they read with no timeout at all.
GRILL_READ_TIMEOUT = 140.0

# A single SSE line the server would ever send is a JSON frame of a few KB.
# The decoder holds the trailing partial line until its newline arrives, so a
# stream that sends no newline at all would otherwise grow that buffer without
# limit for the whole life of a long-running TUI.
MAX_SSE_LINE = 1_000_000


class NhServerUnreachable(Exception):
    """No server answered. Carries the message a human should see."""


class NhApiError(Exception):
    """The server answered, and said no."""


def unreachable_message(base_url: str = DEFAULT_BASE_URL) -> str:
    """What to print instead of a connection traceback."""
    return (
        f"no_human is not running at {base_url}\n"
        "Start it first:\n"
        "  nh start"
    )


# --------------------------------------------------------------------------- #
# SSE                                                                          #
# --------------------------------------------------------------------------- #

class SSEDecoder:
    """Turns a byte-boundary-agnostic stream of text into JSON frames.

    Ported from `grillStepSSE` in web/src/api.js: split on newlines, keep the
    trailing partial line in a buffer, take only ``data: `` lines, and skip a
    line that will not parse rather than aborting the stream. ``id:`` lines
    (the EventSource resume cursor the task-event stream emits) and ``:``
    comments are not payload.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> list[dict[str, Any]]:
        self._buffer += chunk
        lines = self._buffer.split("\n")
        self._buffer = lines.pop()
        if len(self._buffer) > MAX_SSE_LINE:
            # Nothing this protocol emits is this long without a newline, so
            # the partial line is not a frame in progress. Drop it rather than
            # hold it forever; the tail simply fails to parse and is skipped
            # like any other malformed line.
            self._buffer = ""
        frames: list[dict[str, Any]] = []
        for raw in lines:
            line = raw.rstrip("\r")
            if not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[6:])
            except (ValueError, RecursionError):
                # RecursionError is NOT a ValueError: json.loads raises it on
                # deeply nested input, and catching only ValueError lets one
                # hostile frame kill the whole stream.
                continue
            if isinstance(data, dict):
                frames.append(data)
        return frames


def classify_grill_frame(frame: dict[str, Any]) -> str:
    """One of done / eval / result / question / error / event.

    Same split grillStepSSE makes, so the CLI and the web composer cannot read
    the same stream differently. The composer collapses result+question into
    one `onResult`; the CLI needs them apart (a question asks the human, a
    result offers a task), so they are two labels here.
    """
    kind = frame.get("kind")
    if kind == "done":
        return "done"
    if kind == "eval_verdict":
        return "eval"
    if kind == "grill_result":
        return "result"
    if kind == "grill_question":
        return "question"
    if kind == "error":
        return "error"
    return "event"


def _render_validation_errors(detail: Any) -> str:
    """FastAPI's 422 ``detail`` is a LIST of ``{loc, msg, ...}``, never a str.

    Rendering only the status code for it is how a client body the server
    refuses reaches an operator undiagnosed: `POST /api/tasks -> 422` names no
    field, so there is nothing to go and look at. Returns "" for any other
    shape, so the caller falls back to the plain line.
    """
    if not isinstance(detail, list) or not detail:
        return ""
    lines = []
    for item in detail:
        if not isinstance(item, dict):
            lines.append(f"  {item}")
            continue
        loc = item.get("loc")
        parts = [str(p) for p in loc if p != "body"] if isinstance(loc, list) else []
        field = ".".join(parts) or "(body)"
        lines.append(f"  {field}: {item.get('msg') or 'was rejected'}")
    return "\n".join(lines)


def _without_nulls(**fields: Any) -> dict[str, Any]:
    """Only the fields that have a value.

    A key set to None is NOT the same as an absent key. The server declares
    ``kind: str = "feature"`` and ``priority: str = "medium"`` — non-optional
    with a default — and Pydantic refuses an explicit null for a non-optional
    ``str``. Sending every optional key unconditionally therefore 422'd every
    create the shell ever attempted. Omitting lets the server's own defaults
    stand, which is what a client that has no opinion should do.
    """
    return {name: value for name, value in fields.items() if value is not None}


# --------------------------------------------------------------------------- #
# Client                                                                       #
# --------------------------------------------------------------------------- #

class NhClient:
    """Every server call the shell makes. One httpx client, reused."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self.base_url, transport=transport, timeout=timeout,
        )

    async def __aenter__(self) -> NhClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- plumbing ---------------------------------------------------------- #

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            body = None
        call = (f"{response.request.method} {response.request.url.path} "
                f"-> {response.status_code}")
        if isinstance(body, dict):
            detail = body.get("detail")
            if isinstance(detail, str):
                return detail
            fields = _render_validation_errors(detail)
            if fields:
                return f"{call}\n{fields}"
        return call

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            response = await self._http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise NhServerUnreachable(unreachable_message(self.base_url)) from exc
        if response.status_code >= 400:
            raise NhApiError(self._detail(response))
        return response

    async def _json(self, method: str, path: str, **kw: Any) -> Any:
        return (await self._request(method, path, **kw)).json()

    # -- reads -------------------------------------------------------------- #

    async def ping(self) -> bool:
        """Is a no_human server answering? Never raises."""
        try:
            await self._request("GET", "/api/tasks")
        except (NhServerUnreachable, NhApiError):
            return False
        return True

    async def board(self) -> list[dict[str, Any]]:
        data = await self._json("GET", "/api/tasks")
        return data if isinstance(data, list) else []

    async def task(self, task_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/api/tasks/{task_id}")

    async def diff(self, task_id: str) -> str:
        return (await self._request("GET", f"/api/tasks/{task_id}/diff")).text

    async def events(self, task_id: str) -> list[dict[str, Any]]:
        data = await self._json("GET", f"/api/tasks/{task_id}/events")
        return data if isinstance(data, list) else []

    # -- writes ------------------------------------------------------------- #

    async def act(self, task_id: str, verb: str) -> dict[str, Any]:
        """approve / pause / resume / cancel / retry — the no-body POSTs."""
        return await self._json("POST", f"/api/tasks/{task_id}/{verb}")

    async def reply(self, task_id: str, answer: str) -> dict[str, Any]:
        return await self._json("POST", f"/api/tasks/{task_id}/reply",
                                json={"answer": answer})

    async def create_task(
        self,
        *,
        title: str,
        description: str | None = None,
        repo_path: str | None = None,
        project_id: str | None = None,
        kind: str | None = None,
        priority: str | None = None,
        acceptance_criteria: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        body = {
            "title": title,
            "acceptance_criteria": list(acceptance_criteria or []),
            # The server's allowlist maps anything outside board/jira/mcp to
            # "board" — sending "cli" is honest about the surface and lands as
            # "board" server-side either way.
            "source": "cli",
            **_without_nulls(
                description=description,
                repo_path=repo_path,
                project_id=project_id,
                kind=kind,
                priority=priority,
            ),
        }
        return await self._json("POST", "/api/tasks", json=body)

    # -- streams ------------------------------------------------------------ #

    async def _stream(
        self, method: str, path: str, *, timeout: Any, **kw: Any
    ) -> AsyncIterator[dict[str, Any]]:
        decoder = SSEDecoder()
        try:
            async with self._http.stream(method, path, timeout=timeout, **kw) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise NhApiError(self._detail(response))
                async for chunk in response.aiter_text():
                    for frame in decoder.feed(chunk):
                        yield frame
        except httpx.HTTPError as exc:
            raise NhServerUnreachable(unreachable_message(self.base_url)) from exc

    def grill_stream(
        self,
        *,
        title: str,
        description: str | None = None,
        repo_path: str | None = None,
        project_id: str | None = None,
        qa_history: Iterable[dict[str, str]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """POST /api/grill/stream — the SAME intake the web composer runs.

        `GrillStepRequest` does declare its optionals as ``| None``, so a null
        here would be accepted — the omission is deliberate anyway: one rule
        for every body this client builds is what stops the create-task bug
        from growing a second instance.
        """
        body = {
            "title": title,
            "qa_history": list(qa_history or []),
            **_without_nulls(
                description=description,
                repo_path=repo_path,
                project_id=project_id,
            ),
        }
        return self._stream("POST", "/api/grill/stream", json=body,
                            timeout=httpx.Timeout(15.0, read=GRILL_READ_TIMEOUT))

    def stream_events(
        self, task_id: str, *, last_event_id: str = "",
    ) -> AsyncIterator[dict[str, Any]]:
        """GET /api/tasks/{id}/events/stream — the live tool-call tail.

        ``last_event_id`` is the resume cursor the server reads out of the
        `last-event-id` header (the same one a browser's EventSource sends).
        Reconnecting without it replays the whole 200-event deque.
        """
        headers = {"last-event-id": last_event_id} if last_event_id else None
        return self._stream("GET", f"/api/tasks/{task_id}/events/stream",
                            headers=headers,
                            timeout=httpx.Timeout(15.0, read=None))
