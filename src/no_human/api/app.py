"""FastAPI board + approval API for no_human.

Exposes:
  GET  /api/tasks            — board overview (all tasks, summarised)
  GET  /api/tasks/{id}       — full task detail + attempts + review checklist
  GET  /api/tasks/{id}/diff  — git diff for the latest attempt's commit
  POST /api/tasks/{id}/approve   — record human approval (agent never merges)
  POST /api/tasks/{id}/send-back — store feedback, reset task for retry
  WS   /ws                   — live board updates (sync every 2 s)

Static files (the React board) are served from ../../../web/dist when present.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from ..config import load_config
from ..core.db import Store
from ..core.task import Task, TaskStatus
from .models import AttemptOut, BoardPayload, SendBackRequest, TaskOut, TaskSummaryOut

_WEB_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    store = await Store(config.db_path).connect()
    app.state.store = store
    app.state.config = config
    yield
    await store.close()


app = FastAPI(title="no_human board", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Connection manager for WebSocket broadcasts                                  #
# --------------------------------------------------------------------------- #

class _ConnMgr:
    def __init__(self) -> None:
        self._sockets: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._sockets.append(ws)

    def remove(self, ws: WebSocket) -> None:
        if ws in self._sockets:
            self._sockets.remove(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        text = json.dumps(payload)
        dead: list[WebSocket] = []
        for sock in list(self._sockets):
            try:
                await sock.send_text(text)
            except Exception:  # noqa: BLE001
                dead.append(sock)
        for sock in dead:
            self.remove(sock)


_mgr = _ConnMgr()


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _store(req: Request) -> Store:
    return req.app.state.store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _require_task(store: Store, task_id: str) -> Task:
    task = await store.find_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id!r} not found")
    return task


def _latest_pr_url(attempts: list[dict]) -> str | None:
    for a in reversed(attempts):
        if a.get("pr_url"):
            return a["pr_url"]
    return None


async def _board_tasks(store: Store) -> list[TaskSummaryOut]:
    tasks = await store.list_tasks()
    out = []
    for task in tasks:
        attempts = await store.list_attempts(task.id)
        out.append(TaskSummaryOut.from_task(task, _latest_pr_url(attempts)))
    return out


def _git_diff(repo_path: str, commit_sha: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "diff", f"{commit_sha}~1..{commit_sha}", "--no-color"],
            cwd=repo_path, capture_output=True, text=True, timeout=10,
        )
        return proc.stdout[:32000] if proc.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# REST endpoints                                                               #
# --------------------------------------------------------------------------- #

@app.get("/api/tasks", response_model=list[TaskSummaryOut])
async def list_tasks(request: Request) -> list[TaskSummaryOut]:
    return await _board_tasks(_store(request))


@app.get("/api/tasks/{task_id}", response_model=TaskOut)
async def get_task(task_id: str, request: Request) -> TaskOut:
    store = _store(request)
    task = await _require_task(store, task_id)
    attempts = await store.list_attempts(task.id)
    return TaskOut.from_task(task, attempts)


@app.get("/api/tasks/{task_id}/diff", response_class=PlainTextResponse)
async def get_diff(task_id: str, request: Request) -> str:
    store = _store(request)
    task = await _require_task(store, task_id)
    if not task.repo_path:
        return ""
    attempts = await store.list_attempts(task.id)
    for a in reversed(attempts):
        sha = a.get("commit_sha")
        if sha:
            return _git_diff(task.repo_path, sha)
    return ""


@app.post("/api/tasks/{task_id}/approve")
async def approve_task(task_id: str, request: Request) -> dict[str, Any]:
    """Record human approval. NOTE: the agent never merges — human merges the PR."""
    store = _store(request)
    task = await _require_task(store, task_id)
    if task.status != TaskStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=f"task is {task.status.value!r}, not awaiting_approval",
        )
    ctx = task.context or {}
    ctx["approved_at"] = _now()
    task.context = ctx
    await store.update_task(task)
    tasks = await _board_tasks(store)
    await _mgr.broadcast({
        "type": "task_approved",
        "task_id": task.id,
        "tasks": [t.model_dump() for t in tasks],
    })
    return {
        "ok": True,
        "message": "Approval recorded. Merge the PR in your git host — the agent never merges.",
    }


@app.post("/api/tasks/{task_id}/send-back")
async def send_back(
    task_id: str, body: SendBackRequest, request: Request
) -> dict[str, Any]:
    """Return the task to IMPLEMENTING for the next daemon run."""
    store = _store(request)
    task = await _require_task(store, task_id)
    ctx = task.context or {}
    feedback = ctx.get("send_back_feedback") or []
    feedback.append({"at": _now(), "message": body.message})
    ctx["send_back_feedback"] = feedback
    task.context = ctx
    await store.update_task(task)
    # Reset to IMPLEMENTING so the next `nh watch <id>` retries.
    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)
    tasks = await _board_tasks(store)
    await _mgr.broadcast({
        "type": "task_updated",
        "task_id": task.id,
        "status": TaskStatus.IMPLEMENTING.value,
        "tasks": [t.model_dump() for t in tasks],
    })
    return {
        "ok": True,
        "message": "Feedback stored. Run `nh watch <id>` to retry.",
    }


# --------------------------------------------------------------------------- #
# WebSocket — live board (polls DB every 2 s, broadcasts on change)           #
# --------------------------------------------------------------------------- #

@app.websocket("/ws")
async def ws_board(ws: WebSocket) -> None:
    await _mgr.connect(ws)
    store: Store = ws.app.state.store
    try:
        # Initial snapshot.
        tasks = await _board_tasks(store)
        await ws.send_text(json.dumps({
            "type": "init",
            "tasks": [t.model_dump() for t in tasks],
        }))
        # Sync loop: detect DB changes every 2 s and push if changed.
        prev_statuses: dict[str, str] = {t.id: t.status for t in tasks}
        while True:
            await asyncio.sleep(2)
            tasks = await _board_tasks(store)
            curr_statuses = {t.id: t.status for t in tasks}
            if curr_statuses != prev_statuses:
                await ws.send_text(json.dumps({
                    "type": "sync",
                    "tasks": [t.model_dump() for t in tasks],
                }))
                prev_statuses = curr_statuses
    except WebSocketDisconnect:
        _mgr.remove(ws)
    except Exception:  # noqa: BLE001
        _mgr.remove(ws)


# --------------------------------------------------------------------------- #
# Serve the React SPA (if built)                                               #
# --------------------------------------------------------------------------- #

if _WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(_WEB_DIST / "assets")), name="assets")

    @app.get("/", include_in_schema=False)
    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str = "") -> FileResponse:  # noqa: ARG001
        return FileResponse(str(_WEB_DIST / "index.html"))
