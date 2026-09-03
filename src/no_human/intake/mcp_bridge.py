"""MCP stdio bridge exposing task_add + task_status (SCRUM-63).

A minimal Model Context Protocol server, run over stdio, that lets an MCP
client (e.g. an editor's agent) create and check no_human tasks through the
EXISTING local HTTP API at ``http://{server.host}:{server.port}``, which
defaults to ``http://127.0.0.1:8420`` — this module never
imports or touches the store, orchestrator, or API endpoints directly, only
calls the HTTP surface they already expose. Exactly two tools: ``task_add``
and ``task_status``. No auth (localhost-only, same trust domain as the web
board), no editor-specific config, no retry/fallback on startup.

``task_add`` POSTs ``source="mcp"`` and ``POST /api/tasks`` (``create_task``
in ``api/app.py``) persists it as first-class (alongside ``"board"`` and
``"jira"``); ``task_add`` returns whatever ``source`` the server actually
stored, which is ``"mcp"`` for tasks created through this bridge.

Run it with either ``nh mcp-serve`` or ``python -m no_human.intake.mcp_bridge``.
"""

from __future__ import annotations

import json
import logging

import httpx
from mcp.server.fastmcp import FastMCP

log = logging.getLogger(__name__)

BASE_URL = "http://127.0.0.1:8420"
_TIMEOUT = 10.0

# Tests inject an httpx.MockTransport here to stub the HTTP layer without a
# real server. None (the default) means "use the real network".
_TRANSPORT: httpx.BaseTransport | None = None

mcp = FastMCP("no_human-mcp-bridge")


def _resolve_base_url() -> str:
    """Base URL of the local API, from ``server.host`` and ``server.port``.

    Falls back to :data:`BASE_URL` when the config is missing or unreadable, so
    a default install behaves exactly as before.
    """
    try:
        from ..config import load_config
        server = load_config(create_if_missing=False).data.get("server") or {}
    except Exception:  # noqa: BLE001 — an unreadable config must not stop the bridge
        return BASE_URL
    host = server.get("host") or "127.0.0.1"
    port = server.get("port") or 8420
    return f"http://{host}:{port}"


def _client() -> httpx.Client:
    return httpx.Client(base_url=BASE_URL, timeout=_TIMEOUT, transport=_TRANSPORT)


@mcp.tool()
def task_add(title: str, description: str, repo_path: str) -> str:
    """Create a no_human task via POST /api/tasks (source="mcp"). Returns
    compact JSON {"task_id": str, "source": str} — source is whatever the
    server actually stored (the "mcp" source is first-class, see module
    docstring)."""
    with _client() as client:
        resp = client.post(
            "/api/tasks",
            json={
                "title": title,
                "description": description,
                "repo_path": repo_path,
                "source": "mcp",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    return json.dumps(
        {"task_id": data["id"], "source": data["source"]}, separators=(",", ":")
    )


@mcp.tool()
def task_status(task_id_or_external_id: str) -> str:
    """Fetch a task's full current state via GET /api/tasks. Resolves by
    task id (or unique id prefix) first; if that 404s, falls back to matching
    external_id across the task list (GET /api/tasks does not index by
    external_id, so this is a client-side scan). Returns the complete task
    object as compact JSON."""
    with _client() as client:
        resp = client.get(f"/api/tasks/{task_id_or_external_id}")
        if resp.status_code == 404:
            listing = client.get("/api/tasks")
            listing.raise_for_status()
            match = next(
                (t for t in listing.json()
                 if t.get("external_id") == task_id_or_external_id),
                None,
            )
            if match is not None:
                resp = client.get(f"/api/tasks/{match['id']}")
        resp.raise_for_status()
        data = resp.json()
    return json.dumps(data, separators=(",", ":"))


def _ensure_api_reachable() -> None:
    """Refuse to start (no retry, no fallback) if the nh API is unreachable."""
    try:
        with _client() as client:
            resp = client.get("/api/tasks")
            resp.raise_for_status()
    except (httpx.HTTPError, OSError) as exc:
        log.error(
            "no_human API unreachable at %s — refusing to start (%s)",
            BASE_URL, exc,
        )
        raise SystemExit(
            f"no_human API unreachable at {BASE_URL}: {exc}"
        ) from exc


def main() -> None:
    global BASE_URL
    BASE_URL = _resolve_base_url()
    _ensure_api_reachable()
    mcp.run()


if __name__ == "__main__":
    main()
