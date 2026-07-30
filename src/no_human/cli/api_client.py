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
        frames: list[dict[str, Any]] = []
        for raw in lines:
            line = raw.rstrip("\r")
            if not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[6:])
            except ValueError:
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
        if isinstance(body, dict) and isinstance(body.get("detail"), str):
            return body["detail"]
        return f"{response.request.method} {response.request.url.path} -> {response.status_code}"

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
            "description": description,
            "repo_path": repo_path,
            "project_id": project_id,
            "kind": kind,
            "priority": priority,
            "acceptance_criteria": list(acceptance_criteria or []),
            # The server's allowlist maps anything outside board/jira/mcp to
            # "board" — sending "cli" is honest about the surface and lands as
            # "board" server-side either way.
            "source": "cli",
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
        """POST /api/grill/stream — the SAME intake the web composer runs."""
        body = {
            "title": title,
            "description": description,
            "repo_path": repo_path,
            "project_id": project_id,
            "qa_history": list(qa_history or []),
        }
        return self._stream("POST", "/api/grill/stream", json=body,
                            timeout=httpx.Timeout(15.0, read=GRILL_READ_TIMEOUT))

    def stream_events(self, task_id: str) -> AsyncIterator[dict[str, Any]]:
        """GET /api/tasks/{id}/events/stream — the live tool-call tail."""
        return self._stream("GET", f"/api/tasks/{task_id}/events/stream",
                            timeout=httpx.Timeout(15.0, read=None))
