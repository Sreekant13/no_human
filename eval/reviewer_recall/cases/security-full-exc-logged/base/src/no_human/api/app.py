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
import copy
import json
import contextlib
import os
import re
import subprocess
import time
from dataclasses import asdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:  # import-cycle-free: the eval package is loaded lazily below
    from ..eval.northstar_card import NorthStarCard

import httpx
from fastapi import (
    FastAPI, File, HTTPException, Request, UploadFile, WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from starlette.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import _atomic_write_text, load_config
from ..core.db import Store
from ..core.orchestrator import is_agent_session
from ..core.task import Task, TaskStatus
from .models import (
    AttemptOut, BoardPayload, CreateProjectRequest, CreateTaskRequest,
    GrillQuestionOut, GrillResultOut, GrillStepRequest, JiraImportedInfo,
    JiraIssueOut, ProjectOut, ReplyRequest, SaveIntegrationConfigRequest,
    SendBackRequest, TaskOut, TaskSummaryOut, UpdateProjectRequest,
)

import logging

log = logging.getLogger("no_human.api")

_WEB_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    store = await Store(config.db_path).connect()
    app.state.store = store
    app.state.config = config

    # Always start the embedded worker — board up = worker up.
    # CLI may override max_workers/poll_interval via app.state._worker_opts.
    from ..agent.claude_backend import ClaudeBackend
    from ..context import ContextGatherer, build_default_sources
    from ..core.orchestrator import Orchestrator
    from ..core.scheduler import Scheduler, resolve_max_workers
    from ..learning import LearningQueue
    from ..notify.slack import SlackNotifier
    from ..review.reviewer import AdversarialReviewer

    def _orch_factory(task=None):
        # Single in-process Claude Agent SDK backend (lean-stack; no alternate
        # backend abstraction).
        backend = ClaudeBackend(
            model=config.primary_model,
            forbidden_paths=config["safety"]["forbidden_paths"],
            never_push_to=config["git"]["never_push_to"],
        )
        review_backend = None  # reviewer defaults to ClaudeBackend(readonly=True)
        notifier = SlackNotifier(config["notifications"].get("slack_webhook_url"))
        gatherer = ContextGatherer(build_default_sources(store, config.data))
        reviewer = AdversarialReviewer(model=config.review_model, backend=review_backend)
        return Orchestrator(
            store, config.data, backend, notifier,
            context_gatherer=gatherer,
            learning_queue=LearningQueue(store),
            reviewer=reviewer,
        )

    overrides = getattr(app.state, "_worker_opts", None) or {}
    conc = config.data.get("concurrency", {})
    max_workers, worker_warning = resolve_max_workers(
        config.data, override=overrides.get("max_workers"))
    if worker_warning:
        log.warning("%s", worker_warning)
    # Bound pytest-xdist so N parallel tasks each running `pytest -n auto`
    # don't oversubscribe the CPU (child test subprocesses inherit this).
    from ..core.scheduler import bounded_xdist_workers
    _cap = bounded_xdist_workers(
        max_workers, os.cpu_count() or 2,
        os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS"))
    if _cap is not None:
        os.environ["PYTEST_XDIST_AUTO_NUM_WORKERS"] = _cap
        log.info("bounded pytest-xdist auto workers to %s (%d task workers)",
                 _cap, max_workers)
    raw_poll = overrides.get("poll_interval") or conc.get("poll_interval", 10)
    try:
        poll_interval = float(raw_poll)
    except (ValueError, TypeError):
        # Handle "10s", "30s" style strings.
        import re as _re
        m = _re.match(r"(\d+)", str(raw_poll))
        poll_interval = float(m.group(1)) if m else 10.0

    # Optional wake watcher for auto-resuming blocked tasks.
    watcher = None
    try:
        from ..blockers import WakeWatcher
        from ..vcs.pr_watcher import (
            check_pr_comments, default_ci_log_excerpt, default_pr_checks,
            default_pr_merged, default_pr_state,
        )
        watcher = WakeWatcher(
            store, config.data,
            pr_merged=default_pr_merged, pr_comment=check_pr_comments,
            pr_state=default_pr_state, pr_checks=default_pr_checks,
            ci_log=default_ci_log_excerpt,
        )
    except Exception as exc:  # noqa: BLE001
        # B2 #13: this used to swallow silently — parked tasks are
        # notify-silent BY DESIGN and depend entirely on the watcher to wake,
        # so a dead watcher meant tasks BLOCKED forever with nobody told.
        # Loud log + a board-visible flag (surfaced via /api/worker).
        log.error("WakeWatcher failed to start — parked tasks will NOT wake "
                  "until the server restarts cleanly: %s", exc)
        app.state.watcher_error = str(exc)[:200]

    # PR-E: periodic re-analysis job (EVOLUTION_PLAN Phase 9).
    reanalysis = None
    ra_cfg = config.data.get("reanalysis", {})
    if ra_cfg.get("enabled", True):
        from ..core.scheduler import ReanalysisJob
        reanalysis = ReanalysisJob(
            store,
            interval_seconds=float(ra_cfg.get("interval_seconds", 86400)),
            days=int(ra_cfg.get("days", 30)),
            max_proposals_per_run=int(ra_cfg.get("max_proposals", 20)),
        )

    sched = Scheduler(
        store, _orch_factory,
        max_workers=max_workers,
        wake_watcher=watcher,
        on_event=lambda k, t: log.info("worker: %s — %s", k, t),
        reanalysis_job=reanalysis,
        config=config.data,
    )
    stop_event = asyncio.Event()
    worker_task = asyncio.create_task(
        sched.run_forever(stop=stop_event, poll_interval=poll_interval)
    )
    app.state.scheduler = sched
    app.state.worker_stop = stop_event
    log.info("embedded worker started: %d worker(s), poll=%ds",
             max_workers, int(poll_interval))

    yield

    if worker_task and stop_event:
        stop_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=30)
        except asyncio.TimeoutError:
            log.warning("worker drain timed out after 30s")
    await store.close()


app = FastAPI(title="no_human board", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# The board is fully self-contained (self-hosted fonts, data: favicon, ws
# socket) — say so on every response, so an injected external script/style/
# frame can never load (electron-pro checklist; fonts+CSP increment). React
# style attributes need 'unsafe-inline' in style-src; scripts stay strict
# (the built index.html has no inline script).
_CSP = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self' ws: wss:; object-src 'none'; "
    "base-uri 'self'; frame-ancestors 'none'"
)


@app.middleware("http")
async def _csp_header(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", _CSP)
    return response


# --------------------------------------------------------------------------- #
# Connection manager for WebSocket broadcasts                                  #
# --------------------------------------------------------------------------- #

class _ConnMgr:
    """B2 #9: every socket has TWO writers (its ws_board poll loop and the
    mutation broadcasts) — unserialized interleaved send_text corrupted
    sockets that then died silently while the client still showed
    "Connected". A per-socket lock serializes all sends."""

    def __init__(self) -> None:
        self._sockets: list[WebSocket] = []
        self._locks: dict[int, asyncio.Lock] = {}

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._sockets.append(ws)
        self._locks[id(ws)] = asyncio.Lock()

    def remove(self, ws: WebSocket) -> None:
        if ws in self._sockets:
            self._sockets.remove(ws)
        self._locks.pop(id(ws), None)

    async def send(self, ws: WebSocket, text: str) -> None:
        lock = self._locks.get(id(ws))
        if lock is None:
            await ws.send_text(text)
            return
        async with lock:
            await ws.send_text(text)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        text = json.dumps(payload)
        dead: list[WebSocket] = []
        for sock in list(self._sockets):
            try:
                await self.send(sock, text)
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


async def _board_tasks(store: Store, scheduler=None) -> list[TaskSummaryOut]:
    tasks = await store.list_tasks()
    # B2 #16: ONE grouped query instead of an N+1 per board tick per socket.
    by_task = await store.attempts_by_task()
    # SCRUM-15: `scheduler.inflight` returns a fresh set() copy per call — snapshot
    # once so every card in this response is judged against the same instant.
    inflight = scheduler.inflight if scheduler is not None else set()
    out = []
    for task in tasks:
        attempts = by_task.get(task.id, [])
        summary = TaskSummaryOut.from_task(
            task, _latest_pr_url(attempts), attempts=attempts,
        )
        if scheduler is not None:
            summary.claimed = task.id in inflight
            ls = scheduler.get_live_status(task.id)
            if ls:
                summary.live_status = ls
            # Subtask progress for compound parents.
            if task.status.value == "compound_parent":
                subs = await store.list_subtasks(task.id)
                if subs:
                    done = sum(1 for s in subs if s.status.value == "done")
                    summary.subtask_progress = f"{done}/{len(subs)}"
        out.append(summary)
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

def _sched(request: Request):
    return getattr(request.app.state, "scheduler", None)


@app.get("/api/tasks", response_model=list[TaskSummaryOut])
async def list_tasks(request: Request) -> list[TaskSummaryOut]:
    return await _board_tasks(_store(request), scheduler=_sched(request))


@app.post("/api/tasks", response_model=TaskSummaryOut, status_code=201)
async def create_task(body: CreateTaskRequest, request: Request) -> TaskSummaryOut:
    """Create a new task from the web board. The task is staged as PENDING and
    will be picked up by the next ``nh serve`` tick or ``nh watch``."""
    store = _store(request)
    repo_path: str | None = None
    linked: list[str] = []
    # Resolve from project if given; project takes precedence over raw repo_path.
    if body.project_id:
        proj = await store.get_project(body.project_id)
        if not proj:
            raise HTTPException(404, f"project {body.project_id!r} not found")
        # If the caller also specified a repo_path that belongs to this project,
        # use it as the target instead of the primary.  This lets the UI's
        # "target repo" picker work for multi-repo projects.
        if body.repo_path and body.repo_path in proj.repo_paths:
            repo_path = body.repo_path
        else:
            repo_path = proj.primary_repo
        linked = [r for r in proj.repo_paths if r != repo_path]
    elif body.repo_path:
        repo = Path(body.repo_path).expanduser().resolve()
        if not repo.is_dir() or not (repo / ".git").exists():
            raise HTTPException(
                status_code=422,
                detail=f"repo_path {body.repo_path!r} is not a git repository",
            )
        repo_path = str(repo)
    # Any value other than the two the web UI actually produces falls back to
    # "board" — never invented, and never trusts an arbitrary client string
    # into Task.source.
    source = body.source if body.source in ("board", "jira") else "board"
    # Jira dedup key (SCRUM-32): only honored for source == "jira"; trim then
    # cap to 64 chars, exact-match only (no case/char normalization).
    external_id: str | None = None
    if source == "jira" and body.external_id is not None:
        external_id = body.external_id.strip()[:64] or None
    task = Task.new(
        title=body.title,
        source=source,
        repo_path=repo_path,
        description=body.description,
        kind=body.kind,
        external_id=external_id,
    )
    task.priority = body.priority
    task.acceptance_criteria = body.acceptance_criteria
    task.linked_repos = linked
    if body.backend and body.backend == "claude":
        task.config["backend"] = body.backend
    if repo_path:
        from ..profile import apply_default_task_config
        profile = await store.get_profile(repo_path)
        task.config = apply_default_task_config(profile, task.config)
    await store.create_task(task)
    summary = TaskSummaryOut.from_task(task)
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({
        "type": "task_created",
        "task_id": task.id,
        "tasks": [t.model_dump() for t in tasks],
    })
    return summary


@app.post("/api/grill")
async def grill_step_endpoint(body: GrillStepRequest, request: Request):
    """B2: Run one step of the intake grill interrogation.

    Phase 4b changes:
      - Uses review_model (Sonnet) instead of primary_model (Opus) — the grill
        is read-only clarification; Sonnet is sufficient and cheaper.
      - Caches the backend in app.state._grill_sessions keyed by (title, repo)
        so multi-round grills reuse the same agent session (context carryover).
    """
    from ..agent.claude_backend import ClaudeBackend
    from ..intake.grill import GrillQuestion, GrillResult, grill_step

    config = request.app.state.config
    store = _store(request)
    repo_path: str | None = None
    if body.project_id:
        proj = await store.get_project(body.project_id)
        if proj:
            if body.repo_path and body.repo_path in proj.repo_paths:
                repo_path = body.repo_path
            else:
                repo_path = proj.primary_repo
    elif body.repo_path:
        repo = Path(body.repo_path).expanduser().resolve()
        if not repo.is_dir() or not (repo / ".git").exists():
            raise HTTPException(
                status_code=422,
                detail=f"repo_path {body.repo_path!r} is not a git repository",
            )
        repo_path = str(repo)

    # Phase 4b: session reuse — cache grill backends by (title, repo).
    grill_sessions = getattr(request.app.state, "_grill_sessions", None)
    if grill_sessions is None:
        grill_sessions = {}
        request.app.state._grill_sessions = grill_sessions
    cache_key = (body.title, repo_path or "")
    backend = grill_sessions.get(cache_key)
    if backend is None:
        # Phase 4b: use review_model (Sonnet) for the grill subagent.
        backend = ClaudeBackend(
            model=config.review_model,
            forbidden_paths=config["safety"]["forbidden_paths"],
            never_push_to=config["git"]["never_push_to"],
            readonly=True,
        )
        grill_sessions[cache_key] = backend
        # Evict oldest if cache grows (prevent unbounded memory).
        if len(grill_sessions) > 20:
            oldest = next(iter(grill_sessions))
            grill_sessions.pop(oldest, None)

    step = await grill_step(
        body.title, body.description, repo_path, body.qa_history, backend,
    )
    if isinstance(step, GrillResult):
        return GrillResultOut(
            title=step.title, description=step.description,
            acceptance_criteria=step.acceptance_criteria,
        )
    return GrillQuestionOut(
        question=step.question, suggestions=step.suggestions, round=step.round,
    )


@app.post("/api/grill/stream")
async def grill_stream_endpoint(body: GrillStepRequest, request: Request):
    """SSE endpoint — streams grill exploration events in real-time.

    Each SSE frame is a JSON object with {ts, kind, text, source}.
    The final frame carries kind="grill_result" or kind="grill_question"
    with the full payload. Falls through to the sync POST semantics on
    the backend — only the transport is different.
    """
    from ..agent.claude_backend import ClaudeBackend
    from ..intake.grill import GrillQuestion, GrillResult, grill_step

    config = request.app.state.config
    store = _store(request)
    repo_path: str | None = None
    if body.project_id:
        proj = await store.get_project(body.project_id)
        if proj:
            if body.repo_path and body.repo_path in proj.repo_paths:
                repo_path = body.repo_path
            else:
                repo_path = proj.primary_repo
    elif body.repo_path:
        repo = Path(body.repo_path).expanduser().resolve()
        if not repo.is_dir() or not (repo / ".git").exists():
            raise HTTPException(
                status_code=422,
                detail=f"repo_path {body.repo_path!r} is not a git repository",
            )
        repo_path = str(repo)

    grill_sessions = getattr(request.app.state, "_grill_sessions", None)
    if grill_sessions is None:
        grill_sessions = {}
        request.app.state._grill_sessions = grill_sessions
    cache_key = (body.title, repo_path or "")
    backend = grill_sessions.get(cache_key)
    if backend is None:
        backend = ClaudeBackend(
            model=config.review_model,
            forbidden_paths=config["safety"]["forbidden_paths"],
            never_push_to=config["git"]["never_push_to"],
            readonly=True,
        )
        grill_sessions[cache_key] = backend
        if len(grill_sessions) > 20:
            oldest = next(iter(grill_sessions))
            grill_sessions.pop(oldest, None)

    queue: asyncio.Queue = asyncio.Queue()

    def _on_event(event):
        """Push agent events into the SSE queue."""
        kind = getattr(event, "kind", "") or ""
        tool = getattr(event, "tool_name", "") or ""
        inp = getattr(event, "tool_input", None) or {}
        text = ""
        if kind == "tool_use" and tool:
            text = _summarize_tool(tool, inp)
        elif kind in ("text", "assistant", "result"):
            text = (getattr(event, "text", "") or "").strip()[:300]
            if not text:
                return
        else:
            return
        frame = {"ts": time.time(), "kind": kind if kind != "tool_use" else "tool_use",
                 "text": text, "source": "grill"}
        queue.put_nowait(frame)

    async def _run_grill():
        try:
            step = await grill_step(
                body.title, body.description, repo_path,
                body.qa_history or [], backend, on_event=_on_event,
            )
            if isinstance(step, GrillResult):
                # D1/D9: run evaluator and emit verdict before grill_result.
                try:
                    from ..intake.evaluator import evaluate_spec
                    eval_result = await evaluate_spec(
                        step.title, step.description, step.acceptance_criteria,
                        model=config.utility_model,
                    )
                    if eval_result:
                        queue.put_nowait({
                            "kind": "eval_verdict", "source": "grill",
                            **eval_result.as_dict(),
                        })
                except Exception:  # noqa: BLE001 — advisory
                    pass
                queue.put_nowait({
                    "kind": "grill_result", "source": "grill",
                    "type": "done", "title": step.title,
                    "description": step.description,
                    "acceptance_criteria": step.acceptance_criteria,
                })
            else:
                queue.put_nowait({
                    "kind": "grill_question", "source": "grill",
                    "type": "question", "question": step.question,
                    "suggestions": step.suggestions, "round": step.round,
                })
        except Exception as exc:
            queue.put_nowait({"kind": "error", "text": str(exc), "source": "grill"})
        finally:
            queue.put_nowait(None)  # sentinel

    async def _generate():
        task = asyncio.create_task(_run_grill())
        try:
            while True:
                frame = await asyncio.wait_for(queue.get(), timeout=130)
                if frame is None:
                    yield "data: {\"kind\": \"done\", \"text\": \"stream ended\"}\n\n"
                    return
                yield f"data: {json.dumps(frame)}\n\n"
        except asyncio.TimeoutError:
            yield "data: {\"kind\": \"done\", \"text\": \"stream timeout\"}\n\n"
        finally:
            task.cancel()

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/tasks/{task_id}", response_model=TaskOut)
async def get_task(task_id: str, request: Request) -> TaskOut:
    store = _store(request)
    task = await _require_task(store, task_id)
    attempts = await store.list_attempts(task.id)
    out = TaskOut.from_task(task, attempts)
    # SCRUM-16: same claimed contract as the board summaries (SCRUM-15) — the
    # slide-over must know whether a live session actually holds this task.
    sched = _sched(request)
    if sched is not None:
        out.claimed = task.id in sched.inflight
    return out


@app.get("/api/tasks/{task_id}/subtasks", response_model=list[TaskSummaryOut])
async def list_subtasks(task_id: str, request: Request) -> list[TaskSummaryOut]:
    store = _store(request)
    subs = await store.list_subtasks(task_id)
    out = []
    for t in subs:
        attempts = await store.list_attempts(t.id)
        out.append(TaskSummaryOut.from_task(t, _latest_pr_url(attempts), attempts=attempts))
    return out


@app.get("/api/tasks/{task_id}/diff", response_class=PlainTextResponse)
async def get_diff(task_id: str, request: Request) -> str:
    store = _store(request)
    task = await _require_task(store, task_id)
    # For code_review tasks, the PR diff is stored in context.
    ctx = task.context or {}
    if ctx.get("pr_diff"):
        return ctx["pr_diff"]
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
    task.context = await store.merge_context(task.id, {"approved_at": _now()})
    # An already-satisfied claim has no PR to merge — approval IS the human
    # confirmation its terminal promised, so it completes the task (the agent
    # still never merges anything; there is nothing to merge). Guarded on
    # pr_url: the report key persists in context, and after a send-back a
    # LATER attempt may ship a real PR — that approval must stay a merge
    # instruction, never a false DONE (PR #101 round-2 review).
    message = "Approval recorded. Merge the PR in your git host — the agent never merges."
    if (task.context or {}).get("already_satisfied_report"):
        attempts = await store.list_attempts(task.id)
        has_pr = any(a.get("pr_url") for a in attempts)
        if not has_pr:
            await store.set_status(task, TaskStatus.DONE, validate=False)
            message = ("Already satisfied claim confirmed — no code change was "
                       "needed. Task done (there is no PR; the agent never merges).")
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({
        "type": "task_approved",
        "task_id": task.id,
        "tasks": [t.model_dump() for t in tasks],
    })
    return {
        "ok": True,
        "message": message,
    }


@app.post("/api/tasks/{task_id}/finish-review")
async def finish_review(task_id: str, request: Request) -> dict[str, Any]:
    """Mark a code-review task done — the human has posted the comments they
    want (all, some, or none) and is finished. A code_review has no PR of its own
    to merge, so it never auto-completes; without this it stays stuck in Review
    PR even after the human is done. This is the explicit 'I'm done' action."""
    store = _store(request)
    task = await _require_task(store, task_id)
    if task.status != TaskStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=f"task is {task.status.value!r}, not awaiting_approval",
        )
    drafts = (task.context or {}).get("draft_review_comments") or []
    posted = sum(1 for d in drafts if d.get("posted"))
    await store.set_status(task, TaskStatus.DONE, validate=False)
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({
        "type": "task_updated",
        "task_id": task.id,
        "tasks": [t.model_dump() for t in tasks],
    })
    return {
        "ok": True,
        "posted": posted,
        "total": len(drafts),
        "message": f"Review finished — {posted}/{len(drafts)} comment(s) posted.",
    }


_ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024  # 20MB — a screenshot/doc, not a dataset


@app.post("/api/tasks/{task_id}/attachments")
async def add_attachment(
    task_id: str, request: Request, file: UploadFile = File(...),
) -> dict[str, Any]:
    """Attach a screenshot/document to a task. Stored on disk under
    ~/.no_human/attachments/<task_id>/ (files, not SQLite blobs — lean stack);
    the path is recorded on task.context so the coder can READ it for context
    (a screenshot of the bug, a design doc, an error log)."""
    import re as _re

    from ..config import NO_HUMAN_HOME
    store = _store(request)
    task = await _require_task(store, task_id)
    data = await file.read()
    if len(data) > _ATTACHMENT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="attachment exceeds 20MB")
    # Sanitize the name — no path traversal, no separators.
    safe = _re.sub(r"[^A-Za-z0-9._-]", "_", Path(file.filename or "attachment").name)
    dest_dir = NO_HUMAN_HOME / "attachments" / _re.sub(r"[^A-Za-z0-9_-]", "", task_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe
    dest.write_bytes(data)
    attachments = list((task.context or {}).get("attachments") or [])
    attachments.append({"name": safe, "path": str(dest)})
    task.context = await store.merge_context(task.id, {"attachments": attachments})
    return {"ok": True, "name": safe, "path": str(dest), "count": len(attachments)}


@app.post("/api/tasks/{task_id}/send-back")
async def send_back(
    task_id: str, body: SendBackRequest, request: Request
) -> dict[str, Any]:
    """Return the task to IMPLEMENTING for the next daemon run."""
    store = _store(request)
    task = await _require_task(store, task_id)
    await store.append_context_list(
        task.id, "send_back_feedback", {"at": _now(), "message": body.message})
    # Reset to IMPLEMENTING so the next `nh watch <id>` retries.
    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)
    tasks = await _board_tasks(store, scheduler=_sched(request))
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


_PARKED_STATUSES = {
    TaskStatus.BLOCKED, TaskStatus.AWAITING_INPUT,
    TaskStatus.PAUSED_QUOTA, TaskStatus.ESCALATED,
}

_ACTIVE_STATUSES = {
    TaskStatus.CONTEXT, TaskStatus.PLANNING, TaskStatus.IMPLEMENTING,
    TaskStatus.REVIEWING, TaskStatus.TESTING,
}


@app.post("/api/tasks/{task_id}/pause")
async def pause_task(
    task_id: str, request: Request,
) -> dict[str, Any]:
    """Pause a running task (sets to BLOCKED with reason)."""
    store = _store(request)
    task = await _require_task(store, task_id)
    if task.status not in _ACTIVE_STATUSES and task.status != TaskStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"task is {task.status.value!r} — only active tasks can be paused",
        )
    task.blocker = {"category": "USER_PAUSED", "question": "Paused from board",
                    "root_cause_hypothesis": "Paused by operator via web board"}
    await store.update_task_columns(task)
    await store.set_status(task, TaskStatus.BLOCKED, validate=False)
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({"type": "task_updated", "task_id": task.id,
                          "tasks": [t.model_dump() for t in tasks]})
    return {"ok": True, "message": f"Paused {task_id[:8]}"}


@app.post("/api/tasks/{task_id}/resume")
async def resume_task(
    task_id: str, request: Request,
) -> dict[str, Any]:
    """Resume a paused/blocked/escalated task (sets to IMPLEMENTING)."""
    store = _store(request)
    task = await _require_task(store, task_id)
    if task.status not in _PARKED_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"task is {task.status.value!r} — only parked tasks can be resumed",
        )
    task.blocker = None
    task.wake_check_at = None
    await store.update_task_columns(task)
    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({"type": "task_updated", "task_id": task.id,
                          "tasks": [t.model_dump() for t in tasks]})
    return {"ok": True, "message": f"Resumed {task_id[:8]} → implementing"}


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str, request: Request,
) -> dict[str, Any]:
    """Cancel a task (sets to FAILED)."""
    store = _store(request)
    task = await _require_task(store, task_id)
    if task.status in {TaskStatus.DONE, TaskStatus.FAILED}:
        raise HTTPException(
            status_code=409,
            detail=f"task is already {task.status.value!r}",
        )
    task.context = await store.merge_context(
        task.id, {"cancel_reason": "Cancelled from board"})
    await store.set_status(task, TaskStatus.FAILED, validate=False)
    # Cancel must STOP the work, not just flip the status. A running task's SDK
    # (claude) and pytest subprocesses live under its worktree and would keep
    # churning + holding resources otherwise (a cancelled task left 3 orphans).
    # The 32-hex task id appears in those command lines, so kill by it. Best-
    # effort — never let cleanup failure break the cancel response.
    await _kill_task_processes(task.id)
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({"type": "task_updated", "task_id": task.id,
                          "tasks": [t.model_dump() for t in tasks]})
    return {"ok": True, "message": f"Cancelled {task_id[:8]}"}


async def _kill_task_processes(task_id: str) -> int:
    """Best-effort kill of a task's worktree subprocesses (SDK + pytest) by its
    unique id. Returns how many pkill patterns matched (for tests/telemetry)."""
    if not task_id or len(task_id) < 12:  # never pkill on a too-broad pattern
        return 0
    try:
        proc = await asyncio.to_thread(
            subprocess.run, ["pkill", "-9", "-f", task_id],
            capture_output=True, timeout=10,
        )
        return 1 if proc.returncode in (0, 1) else 0  # 1 = no match, still fine
    except Exception:  # noqa: BLE001
        log.debug("cancel: worktree process cleanup best-effort failed", exc_info=True)
        return 0


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(
    task_id: str, request: Request,
) -> dict[str, Any]:
    """Retry a failed task (resets to PENDING for a fresh run)."""
    store = _store(request)
    task = await _require_task(store, task_id)
    if task.status != TaskStatus.FAILED:
        raise HTTPException(
            status_code=409,
            detail=f"task is {task.status.value!r} — only failed tasks can be retried",
        )
    task.blocker = None
    task.wake_check_at = None
    # None deletes the key (RFC 7396) — clears cancel_reason atomically.
    task.context = await store.merge_context(
        task.id, {"cancel_reason": None, "retried_at": _now()})
    await store.update_task_columns(task)
    await store.set_status(task, TaskStatus.PENDING, validate=False)
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({"type": "task_updated", "task_id": task.id,
                          "tasks": [t.model_dump() for t in tasks]})
    return {"ok": True, "message": f"Retried {task_id[:8]} → pending"}


class PostReviewCommentsRequest(BaseModel):
    items: list[int] | None = None  # indices of items to post; None = all failed


def _parse_pr_url(url: str) -> tuple[str, str, str, int]:
    """Parse a GHE/GitHub PR URL → (hostname, owner, repo, pr_number)."""
    import re
    m = re.match(
        r"https?://([^/]+)/([^/]+)/([^/]+)/pull/(\d+)",
        url,
    )
    if not m:
        raise ValueError(f"cannot parse PR URL: {url}")
    return m.group(1), m.group(2), m.group(3), int(m.group(4))


@app.post("/api/tasks/{task_id}/post-review-comments")
async def post_review_comments(
    task_id: str, body: PostReviewCommentsRequest, request: Request,
) -> dict[str, Any]:
    """Post review comments to the PR via gh api on behalf of the human."""
    store = _store(request)
    task = await _require_task(store, task_id)

    ctx = task.context or {}
    pr_url = ctx.get("pr_url")            # anchor / fallback for unmatched files
    pr_files = ctx.get("pr_files") or {}  # {url: [files]} — routes each finding to its PR/MR
    if not pr_url and not pr_files:
        raise HTTPException(400, "no PR URL stored for this task")

    # Get the checklist from the latest attempt.
    attempts = await store.list_attempts(task.id)
    checklist = None
    for a in reversed(attempts):
        cl = a.get("review_checklist")
        if cl:
            checklist = json.loads(cl) if isinstance(cl, str) else cl
            break
    if not checklist or not checklist.get("items"):
        raise HTTPException(400, "no review checklist found")

    items = checklist["items"]
    if body.items is not None:
        indices = [i for i in body.items if 0 <= i < len(items)]
    else:
        indices = [i for i, it in enumerate(items) if not it.get("passed")]
    if not indices:
        return {"ok": True, "posted": 0, "results": []}

    # Route each finding to the change set that owns its file, and post via that
    # forge's API — a cross-repo review spans GitHub Enterprise AND GitLab, so a
    # a finding on a GitLab-hosted file must land on that MR (glab), not the GHE PR (gh).
    from ..vcs.comment_poster import pick_pr_for_file, post_to_pr

    results = []
    for idx in indices:
        item = items[idx]
        file_path = item.get("file", "") or ""
        line = item.get("line", 0) or 0
        comment = item.get("comment") or item.get("evidence", "")
        if not comment:
            results.append({"index": idx, "ok": False, "error": "no comment text"})
            continue
        target = pick_pr_for_file(file_path, pr_files, pr_url)
        if not target:
            results.append({"index": idx, "ok": False, "error": "no PR to post to"})
            continue
        res = await asyncio.to_thread(
            post_to_pr, target, comment, file_path or None, line if line > 0 else None,
        )
        entry = {"index": idx, "ok": res["ok"], "pr": target, "mode": res.get("mode")}
        if not res["ok"]:
            entry["error"] = res.get("error", "")[:300]
        results.append(entry)

    posted = sum(1 for r in results if r["ok"])
    return {"ok": posted > 0, "posted": posted, "total": len(indices), "results": results}


@app.get("/api/profiles")
async def list_profiles(request: Request) -> list[dict[str, Any]]:
    """Return onboarded repo profiles (for the New Task repo dropdown).

    When no profiles exist, falls back to distinct repo_paths from existing
    tasks so the repo picker has something to show.
    """
    store = _store(request)
    try:
        rows = await store.list_profiles()
    except Exception:  # noqa: BLE001 — table may not exist yet
        rows = []
    if rows:
        return [{"repo_path": r.get("repo_path", ""),
                 "ecosystem": r.get("ecosystem", ""),
                 "confirmed": bool(r.get("confirmed", False)),
                 "name": r.get("repo_path", "").rstrip("/").rsplit("/", 1)[-1] if r.get("repo_path") else ""}
                for r in rows]
    # Fallback: unique repo_paths from existing tasks.
    tasks = await store.list_tasks()
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for t in tasks:
        rp = t.repo_path
        if rp and rp not in seen:
            seen.add(rp)
            out.append({"repo_path": rp, "ecosystem": "", "confirmed": False,
                        "name": rp.rstrip("/").rsplit("/", 1)[-1]})
    return out


async def _known_repo_paths(store) -> set[str]:
    """Every repo the operator already knows: onboarded profiles + any repo a
    task references. This set is the allow-list for /api/repo — it is why the
    endpoint can render a repo map without ever walking an arbitrary path the
    caller supplies (which would leak any filesystem tree)."""
    known: set[str] = set()
    try:
        for r in await store.list_profiles():
            rp = (r.get("repo_path") or "").rstrip("/")
            if rp:
                known.add(rp)
    except Exception:  # noqa: BLE001 — table may not exist yet
        pass
    for t in await store.list_tasks():
        if t.repo_path:
            known.add(t.repo_path.rstrip("/"))
    return known


@app.get("/api/repos")
async def api_repos(request: Request) -> list[dict[str, Any]]:
    """The repos the operator knows (for the repo-understanding picker)."""
    store = _store(request)
    out = []
    for rp in sorted(await _known_repo_paths(store)):
        out.append({"repo_path": rp, "name": rp.rsplit("/", 1)[-1] or rp})
    return out


@app.get("/api/repo")
async def api_repo_understanding(request: Request, path: str) -> dict[str, Any]:
    """What no_human understands about ONE known repo: its onboarded profile,
    the cached repo map, and matched playbooks. Read-only. The path MUST be a
    repo the operator already onboarded or has a task for — an unknown path is
    a 404, never a fresh map of an arbitrary directory."""
    store = _store(request)
    norm = (path or "").rstrip("/")
    if not norm or norm not in await _known_repo_paths(store):
        raise HTTPException(status_code=404, detail="unknown repo")
    from ..context.repo_map import repo_map as _repo_map
    from pathlib import Path as _Path
    prof = await store.get_profile(norm)
    playbooks = await store.list_playbooks(project=norm)
    # repo_map walks the tree + shells out to git — offload it so a large or
    # stale-mount repo can never block the single-threaded event loop (the same
    # asyncio.to_thread discipline the rest of this file uses for blocking work).
    rmap = await asyncio.to_thread(_repo_map, _Path(norm))
    return {
        "repo_path": norm,
        "name": norm.rsplit("/", 1)[-1] or norm,
        "profile": prof.to_dict() if prof else None,
        "repo_map": rmap,
        "playbooks": [
            {"title": p.get("title", ""), "procedure": p.get("procedure", ""),
             "project": p.get("project")}
            for p in playbooks
        ],
    }


@app.get("/api/search")
async def api_search(request: Request, q: str, limit: int = 30) -> list[dict[str, Any]]:
    """Cross-task full-text search over the failure/fix record (events_fts,
    migration 0006 — attempt_failed / review / blocked / tamper / pr_ci_red /
    escalated / ci_gate_fail). "How was a failure like this handled before?"
    surfaced to the operator. Advisory: hostile FTS5 input (bare operators,
    unbalanced quotes) returns [], never a 500 — mirrors _recall_failures."""
    store = _store(request)
    terms = [t for t in (q or "").split() if t]
    if not terms:
        return []
    # FTS5 treats bare punctuation as operators; quote each term so user text is
    # matched literally and a stray `"`/`*`/`NEAR(` can't form a bad query.
    query = " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
    lim = max(1, min(int(limit or 30), 30))
    try:
        cur = await store.db.execute(
            """SELECT te.task_id,
                      json_extract(te.data, '$.kind'),
                      snippet(events_fts, 0, '', '', '…', 12),
                      te.ts
               FROM events_fts f
               JOIN task_events te ON te.id = f.rowid
               WHERE events_fts MATCH ? ORDER BY rank LIMIT ?""",
            (query, lim),
        )
        rows = await cur.fetchall()
    except Exception:  # noqa: BLE001 — search is advisory, never a 500
        return []
    if not rows:
        return []  # skip the O(all tasks) title scan on an empty result
    # Resolve task titles once, falling back to the id when a task row was since
    # deleted — a dangling fts row must never 500 the endpoint.
    titles: dict[str, str] = {t.id: t.title for t in await store.list_tasks()}
    return [
        {
            # Full id (the client truncates for display): grouping on a truncated
            # id could merge two tasks sharing an 8-char prefix.
            "task_id": str(task_id),
            "task_title": titles.get(str(task_id), str(task_id)[:8]),
            "kind": kind or "event",
            "snippet": snip or "",
        }
        for task_id, kind, snip, ts in rows
    ]


# --------------------------------------------------------------------------- #
# Projects — multi-repo grouping                                              #
# --------------------------------------------------------------------------- #

@app.get("/api/projects")
async def api_list_projects(request: Request) -> list[ProjectOut]:
    store = _store(request)
    projects = await store.list_projects()
    return [ProjectOut.from_project(p) for p in projects]


@app.post("/api/projects", response_model=ProjectOut, status_code=201)
async def api_create_project(
    body: CreateProjectRequest, request: Request
) -> ProjectOut:
    store = _store(request)
    # Validate repo paths.
    for rp in body.repo_paths:
        p = Path(rp).expanduser().resolve()
        if not p.is_dir() or not (p / ".git").exists():
            raise HTTPException(422, f"{rp!r} is not a git repository")
    repo_paths = [str(Path(rp).expanduser().resolve()) for rp in body.repo_paths]
    primary = None
    if body.primary_repo:
        primary = str(Path(body.primary_repo).expanduser().resolve())
    elif repo_paths:
        primary = repo_paths[0]
    from ..project_model import Project
    proj = Project.new(name=body.name, repo_paths=repo_paths, primary_repo=primary)
    try:
        await store.create_project(proj)
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(409, f"project {body.name!r} already exists")
        raise
    return ProjectOut.from_project(proj)


@app.get("/api/projects/{project_id}")
async def api_get_project(project_id: str, request: Request) -> ProjectOut:
    store = _store(request)
    proj = await store.get_project(project_id)
    if not proj:
        raise HTTPException(404, "project not found")
    return ProjectOut.from_project(proj)


_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _require_local_origin(request: Request, *, writing: bool = False) -> None:
    """Refuse a cross-origin call to the credential routes.

    The app sets ``allow_origins=["*"]`` and the server is unauthenticated, so
    without this ANY page the operator visits while `nh serve` is up can PUT
    this endpoint and replace the token that pays for the subscription.

    The host is compared EXACTLY, after parsing. A ``startswith`` prefix test
    looks equivalent and is not: ``http://localhost.evil.com`` starts with
    ``http://localhost``, and that is a domain an attacker registers. That
    exact bug shipped here and was caught with a working drive-by.

    On a WRITE, a missing ``Origin`` is refused too. A browser always sends it
    on a cross-site request, so the legitimate Settings UI is unaffected, and
    it is the one case where a local malicious process or a rebinding proxy
    would otherwise face no check at all.
    """
    origin = request.headers.get("origin")
    if origin is None:
        if writing:
            raise HTTPException(
                403, "this endpoint requires a same-origin browser request")
        return
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or (parts.hostname or "") not in _LOCAL_HOSTS:
        raise HTTPException(403, "cross-origin requests are not allowed here")


def _auth_status_payload(request: Request) -> dict[str, Any]:
    """Which subscription pays, and whether a token is on file.

    Names and booleans ONLY — a token value is never returned (constraint §8),
    so this is safe to render in Settings. ``metered_key_present`` is surfaced
    because a stray ANTHROPIC_API_KEY silently bills the metered API, and a
    human should be able to see that without reading their shell profile.
    It is READ here, never scrubbed: a GET must not mutate the environment.
    """
    from ..config import (
        AuthError,
        active_auth_profile,
        available_auth_profiles,
        profile_token_var,
    )
    from ..config import _read_env_file as _env_file
    cfg = getattr(request.app.state, "config", None)
    data = getattr(cfg, "data", None) or {}
    configured = str((data.get("llm") or {}).get("auth_profile") or "default")
    # The effective billing mode this install is configured for. Default
    # matches config.py's own default and every other consumer (commands.py,
    # backend_check.py) — an install that never set llm.auth_mode is OAuth.
    auth_mode = str((data.get("llm") or {}).get("auth_mode") or "subscription")
    profiles = available_auth_profiles()
    running = active_auth_profile()
    try:
        token_var = profile_token_var(configured)
    except AuthError as exc:
        # A malformed llm.auth_profile on disk must not 500 a GET.
        token_var = f"(invalid profile in config: {exc})"
        # ...and it must not be ECHOED either. This branch already knows the
        # value was REJECTED — and one rejection reason is "that looks like a
        # token, not a profile name". The write guards stop new bad values, but
        # a hand-edited or legacy config.yaml would still render a pasted
        # secret straight into the Settings UI, and into any screenshot or bug
        # report made from it. Constraint §8: names and booleans only.
        configured = "(invalid — redacted)"
    # BOTH sources: a key sitting in .env is invisible to os.environ until the
    # next start, and "see it without reading your shell profile" is the
    # entire point of surfacing this. Shared by metered_key_present and
    # api_key_present so the two can never disagree.
    metered_key_present = bool(
        os.environ.get("ANTHROPIC_API_KEY")
        or _env_file().get("ANTHROPIC_API_KEY"))
    if auth_mode == "api_key":
        # The active billing path IS "api_key" once the server has started
        # under this mode (_assert_api_key_mode stamps _ACTIVE_AUTH_PROFILE =
        # "api_key"): a restart would not change what pays, so it is not
        # required. Anything else running (an OAuth profile, or nothing yet)
        # means a restart is what actually switches billing to the key.
        restart_required = running != "api_key"
    else:
        # What config says pays vs what THIS process actually exported: a
        # long-lived server keeps billing the profile it started with, so
        # reporting only the config value would be a lie.
        restart_required = bool(running and running != configured)
    return {
        "configured_profile": configured,
        "active_profile": running,
        "restart_required": restart_required,
        "token_var": token_var,
        # Always "is an OAuth token on file for the configured profile" —
        # unchanged by auth_mode. In api_key mode a False here is expected
        # and not an error: billing runs on the key, not this token.
        "token_present": configured in profiles,
        "profiles": [{"name": p, "token_present": True} for p in profiles],
        "metered_key_present": metered_key_present,
        # The effective billing mode (frontend ticket renders it).
        "auth_mode": auth_mode,
        # Whether ANTHROPIC_API_KEY resolves at all (env or .env) — same
        # expression as metered_key_present, named for what it means in
        # api_key mode: the credential the BYO-key path bills with.
        "api_key_present": metered_key_present,
        # A token on file is necessary but not sufficient: the Claude Agent SDK
        # shells out to the `claude` CLI for every task. Without it the board
        # loads green and every task fails at launch — surface it here so
        # Settings can warn instead of letting the operator discover it one
        # failed task at a time.
        "backend_cli_present": _backend_cli_present(),
    }


def _backend_cli_present() -> bool:
    """Whether the `claude` CLI the coding backend needs is resolvable.

    Read-only path resolution mirroring the SDK; never spawns the CLI.
    """
    from ..agent.backend_check import find_claude_cli

    return find_claude_cli() is not None


@app.get("/api/auth/status")
async def api_auth_status(request: Request) -> dict[str, Any]:
    _require_local_origin(request)
    return _auth_status_payload(request)


@app.put("/api/auth/token")
async def api_set_auth_token(request: Request) -> dict[str, Any]:
    """Store an OAuth token for a profile. Returns the same shape as status.

    The body is parsed BY HAND rather than through a pydantic model, because a
    pydantic validation error echoes the offending body back verbatim —
    submitting the form with the profile box empty returned
    ``{"input": {"token": "<the real secret>"}}`` in a 422. Constraint §8 says a
    secret is never echoed, and that has to hold for the failure paths too.

    This writes a CLAUDE_CODE_OAUTH_TOKEN[_PROFILE] — a subscription or
    enterprise OAuth token. It is NOT a bring-your-own-API-key path: a metered
    key is refused by `set_profile_token`, per constraint #1.
    """
    from ..config import (
        AuthError,
        SUBSCRIPTION_TOKEN_VAR,
        active_auth_profile,
        profile_token_var,
        set_profile_token,
    )

    _require_local_origin(request, writing=True)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — never surface the raw body
        raise HTTPException(422, "expected a JSON object") from None
    if not isinstance(body, dict):
        raise HTTPException(422, "expected a JSON object")
    profile, token = body.get("profile"), body.get("token")
    # Shapes only — never the values.
    if not isinstance(profile, str) or not isinstance(token, str):
        raise HTTPException(
            422, "both 'profile' and 'token' are required and must be strings")

    try:
        written = set_profile_token(profile, token)
    except AuthError as exc:
        # AuthError messages are written to be human-facing and never contain
        # the token; surfacing one is what makes the Settings form usable.
        raise HTTPException(422, str(exc)) from exc

    payload = _auth_status_payload(request)
    # A restart is needed whenever the RUNNING process is still exporting a
    # different value for the profile it is billing — which is exactly what
    # rotating the active profile's token does, and the case the name-only
    # comparison in the status payload reports as False.
    running = active_auth_profile()
    with contextlib.suppress(AuthError):
        if running and written == profile_token_var(running):
            if os.environ.get(SUBSCRIPTION_TOKEN_VAR) != token:
                payload["restart_required"] = True
    return payload
def _bench_payload(card: "NorthStarCard", refusals: list[str]) -> dict[str, Any]:
    """The wire shape of a bench card. One function so the healthy and the
    unreadable path cannot answer with different keys."""
    agg = card.as_dict()["aggregate"]
    return {
        "label": card.label,
        "created_at": card.created_at,
        **agg,
        # With no gated specs the rate is a CONVENTION (1.0), not a
        # measurement, and the UI renders a bare "100% — denominator unknown".
        # That is the unreadable 100% the numerator/denominator pair exists to
        # kill, one layer down. None renders as "—".
        "honest_escalation_rate": (agg["honest_escalation_rate"]
                                   if agg["escalation_specs"] else None),
        "refusals": refusals,
        "override_reasons": card.override_reasons,
    }


@app.get("/api/bench/latest")
async def api_bench_latest() -> dict[str, Any]:
    """The published north-star bench card, for the Stats surface.

    The card was previously unreachable from the web app entirely — the UI's
    north-star row reads /api/metrics, which carries TASK counters, not bench
    results. A reader therefore could not see the two things that decide whether
    a headline means anything: how much of the corpus went unmeasured, and
    whether the run was even publishable.

    `refusals` is COMPUTED from the stored card on every read rather than
    persisted, so it can never go stale against the rules that produce it. It is
    evaluated with no baseline, so it reports the run's INTRINSIC problems
    (saturation, coverage, too few specs) and not "narrower than the previous
    run" — that comparison needs a baseline this endpoint does not have.

    404 ONLY when nothing has been recorded: the UI must say "no bench run yet"
    rather than render an all-zero card that looks like a catastrophic result.
    A card that exists but cannot be read is NOT a 404 — that would report a
    broken instrument as an idle one. It answers 200 with a zeroed card whose
    `refusals` say so, which is the surface the UI already renders as an alarm.
    """
    from ..eval.northstar_card import (
        RESULTS_DIR, NorthStarCard, published_file, publish_refusals,
    )

    path = RESULTS_DIR / "latest.json"
    if not path.exists():
        raise HTTPException(404, "no published bench run")
    # A card that EXISTS but cannot be read is a louder problem than no card at
    # all, and this is the one project where "the instrument is broken" must
    # never render as "nothing to see". `NorthStarCard.load` swallows OSError
    # and JSONDecodeError alike and returns None, and its per-score access uses
    # hard subscripts, so a truncated file, a chmod-000 file, a schema drift or
    # a non-object all ended as either a 404 saying "no bench run yet" (a lie)
    # or a 500 that blanked the panel. Report it through the surface the UI
    # already renders loudly: the refusals list.
    # BOTH failure shapes are logged, because `load` reports them differently:
    # it SWALLOWS OSError/JSONDecodeError and returns None, while schema drift
    # raises straight through. Either way the endpoint answers the same zeroed
    # card, so without a log there is nothing anywhere to diagnose from — and a
    # genuine programming error inside `load` (a new required BenchScore field
    # with no default) would render forever as "the recorded run could not be
    # read", the one case where this endpoint's own diagnosis is wrong.
    try:
        card = NorthStarCard.load(path)
        if card is None:
            log.error("bench card at %s could not be read (unparseable or "
                      "unreadable)", path)
    except Exception:  # noqa: BLE001 — any schema failure, same verdict
        log.exception("bench card at %s could not be read", path)
        card = None
    # SCRUM-25: `latest.json` is the last PUBLISH CALL, clean or forced;
    # `published_baseline.json` (see `published_file()`) is the last CLEAN
    # one. A repo with no clean publish yet (or an older results dir
    # predating this file) has none — `published`/`latest_run` are then
    # simply absent and the response is exactly what this endpoint always
    # returned.
    baseline_card = None
    pub_path = published_file()
    if pub_path.exists():
        try:
            baseline_card = NorthStarCard.load(pub_path)
        except Exception:  # noqa: BLE001 — same "unreadable, not absent" rule
            log.exception("published baseline at %s could not be read",
                          pub_path)

    if baseline_card is not None:
        payload = _bench_payload(baseline_card, publish_refusals(baseline_card))
        payload["published"] = True
        # A footnote only when latest.json holds a DIFFERENT (necessarily
        # newer — nothing but `bench publish` writes either file, and a clean
        # publish writes both at once) run than the baseline itself.
        if card is not None and card.created_at != baseline_card.created_at:
            payload["latest_run"] = _bench_payload(card, publish_refusals(card))
        return payload

    if card is None:
        # Shaped from an EMPTY card rather than a hand-written key list, so the
        # error payload cannot drift out of the healthy payload's shape when a
        # field is added to the aggregate. A reader hitting a missing key here
        # would see the panel break in exactly the situation it exists to
        # explain.
        return _bench_payload(
            NorthStarCard(label="(unreadable)"),
            [f"the recorded run at {path.name} could not be read — it exists "
             f"but is unreadable or malformed, so no figure below can be "
             f"trusted"])
    return _bench_payload(card, publish_refusals(card))


@app.put("/api/projects/{project_id}")
async def api_update_project(
    project_id: str, body: UpdateProjectRequest, request: Request
) -> ProjectOut:
    store = _store(request)
    proj = await store.get_project(project_id)
    if not proj:
        raise HTTPException(404, "project not found")
    if body.name is not None:
        proj.name = body.name
    if body.repo_paths is not None:
        for rp in body.repo_paths:
            p = Path(rp).expanduser().resolve()
            if not p.is_dir() or not (p / ".git").exists():
                raise HTTPException(422, f"{rp!r} is not a git repository")
        proj.repo_paths = [str(Path(rp).expanduser().resolve()) for rp in body.repo_paths]
    if body.primary_repo is not None:
        proj.primary_repo = str(Path(body.primary_repo).expanduser().resolve())
    elif body.repo_paths is not None and proj.repo_paths:
        proj.primary_repo = proj.repo_paths[0]
    if body.test_layers is not None:
        from ..testing.test_layers import TestLayer
        validated = []
        for ld in body.test_layers:
            try:
                layer = TestLayer.from_dict(ld)
                validated.append(layer.to_dict())
            except Exception as exc:
                raise HTTPException(
                    422, f"invalid test layer {ld.get('name', '?')!r}: {exc}"
                )
        proj.test_layers = json.dumps(validated)
    await store.update_project(proj)
    return ProjectOut.from_project(proj)


@app.delete("/api/projects/{project_id}")
async def api_delete_project(project_id: str, request: Request) -> dict:
    store = _store(request)
    ok = await store.delete_project(project_id)
    if not ok:
        raise HTTPException(404, "project not found")
    return {"ok": True}


def _summarize_tool(tool: str, inp: dict) -> str:
    """Human-readable one-liner for an agent tool call."""
    if tool in ("Read", "View"):
        path = inp.get("file_path") or inp.get("path") or ""
        # Show just filename + parent dir to save space.
        parts = path.rsplit("/", 2)
        short = "/".join(parts[-2:]) if len(parts) >= 2 else path
        return f"Read {short}"
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = inp.get("file_path") or inp.get("path") or ""
        parts = path.rsplit("/", 2)
        short = "/".join(parts[-2:]) if len(parts) >= 2 else path
        return f"Edit {short}"
    if tool in ("Grep", "Search"):
        q = inp.get("query") or inp.get("pattern") or ""
        path = inp.get("path") or inp.get("search_path") or ""
        parts = path.rsplit("/", 2)
        short = "/".join(parts[-2:]) if len(parts) >= 2 else path
        return f'Grep "{q[:60]}" in {short}'
    if tool in ("Glob", "ListDir"):
        pat = inp.get("pattern") or inp.get("path") or ""
        return f"Glob {pat[:80]}"
    if tool in ("Bash", "Terminal"):
        cmd = inp.get("command") or inp.get("cmd") or ""
        return f"Run `{cmd[:120]}`"
    # Fallback: tool name + first key.
    first = next(iter(inp.values()), "") if inp else ""
    return f"{tool} {str(first)[:80]}"


_RESULT_PREVIEW_CAP = 400  # chars of tool output surfaced in the activity feed


def _format_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    pending_tool_use: dict[str, Any] | None = None  # last tool_use awaiting its result
    for e in events:
        source = e.get("source", "")
        kind = e.get("kind", "")

        # Always include orchestrator AND watcher events. The watcher filter
        # gap starved the whole post-PR ladder out of the UI: ci_gate_*/merged/
        # pr_ci_red events (source:"watcher") were dropped here, so the
        # Shepherding stage could never light up despite being wired (found
        # by the 2026-07-11 persona walk: 2015 events served, 0 from watcher).
        if source in ("orchestrator", "watcher", "human") \
                or kind in ("result", "error"):
            entry = {"ts": e.get("ts"), "kind": kind,
                     "text": e.get("text", ""), "source": source}
            # Carry the per-role model map so the System view can label each
            # node with the model that actually ran it.
            if kind == "models" and isinstance(e.get("models"), dict):
                entry["models"] = e["models"]
            # The substance of a supervisor decision lives in `message`, not
            # `text` — dropping it made the Supervisor look like it only ever
            # said "continue"/"correct" while its corrections carried real,
            # actionable guidance.
            if isinstance(e.get("message"), str) and e["message"]:
                entry["message"] = e["message"]
            out.append(entry)
            pending_tool_use = None
            continue

        # Agent tool_use: surface what the agent is doing (file, query, cmd),
        # plus the raw tool name/input so the UI can render file chips, and a
        # placeholder the next tool_result fills in below.
        if is_agent_session(source) and kind == "tool_use":
            tool = e.get("tool_name", "")
            inp = e.get("tool_input") or {}
            detail = _summarize_tool(tool, inp)
            entry = {"ts": e.get("ts"), "kind": "tool_use", "text": detail,
                      "source": source, "tool_name": tool, "tool_input": inp}
            out.append(entry)
            pending_tool_use = entry
            continue

        # Agent tool_result: attach a short preview to the tool_use it answers,
        # instead of silently discarding it — this is the actual output of the
        # call (file contents, grep matches, command stdout, etc.), not just
        # the call itself.
        if is_agent_session(source) and kind == "tool_result":
            text = (e.get("text") or "").strip()
            if text and pending_tool_use is not None:
                if len(text) > _RESULT_PREVIEW_CAP:
                    text = text[:_RESULT_PREVIEW_CAP] + "…"
                pending_tool_use["result_preview"] = text
            pending_tool_use = None
            continue

        # Agent prose (non-empty text blocks): show agent reasoning. Rendered
        # as markdown client-side, so keep a generous cap rather than the old
        # 600-char one-liner truncation.
        if is_agent_session(source) and kind == "text" and (e.get("text") or "").strip():
            text = (e.get("text") or "").strip()
            if len(text) > 4000:
                text = text[:3997] + "..."
            out.append({"ts": e.get("ts"), "kind": "agent_text",
                        "text": text, "source": source})
            pending_tool_use = None
            continue

        # Extended-thinking blocks ("Thought for Ns" in the UI) — previously
        # dropped entirely. Surface them as a distinct, collapsible-by-default
        # kind rather than silently discarding the model's reasoning.
        if is_agent_session(source) and kind == "thinking" and (e.get("text") or "").strip():
            text = (e.get("text") or "").strip()
            if len(text) > 4000:
                text = text[:3997] + "..."
            out.append({"ts": e.get("ts"), "kind": "thinking",
                        "text": text, "source": source})
            pending_tool_use = None
            continue

        # Subagent lifecycle (SDK Agent-tool TaskStarted/Progress/Notification).
        # The System view's agent tree discovers dynamically-spawned subagents
        # from these events (task_id/task_type/status). Dropping them here — as
        # this formatter previously did — meant subagents never appeared on
        # initial load or for finished tasks; only the live SSE stream surfaced
        # them. Mirror the SSE handling so both paths are consistent.
        if is_agent_session(source) and kind.startswith("subagent_"):
            entry = {"ts": e.get("ts"), "kind": kind,
                     "text": (e.get("text") or "").strip()[:300],
                     "source": source}
            for key in ("task_id", "task_type", "status"):
                if key in e:
                    entry[key] = e[key]
            out.append(entry)
            pending_tool_use = None
            continue

    return out


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request) -> list[dict[str, Any]]:
    """Return the complete event log for a task.

    The scheduler's in-memory buffer is a deque(maxlen=_MAX_EVENTS), so serving
    it alone silently truncated long runs: at 321 events the board received the
    last 158 and the Planner — whose events had aged out — vanished from the
    System view mid-run. The persisted copy is complete, so it is the base; the
    buffer only supplies the tail newer than the last flush (a couple of
    seconds' worth) and covers a task the store hasn't seen.
    """
    sched = getattr(request.app.state, "scheduler", None)
    buffered: list[dict[str, Any]] = []
    if sched is not None:
        for tid in list(sched._event_log.keys()):
            if tid.startswith(task_id):
                buffered = sched.task_events(tid)
                break

    store = _store(request)
    task = await store.find_task(task_id)
    persisted = await store.list_events(task.id) if task is not None else []
    if not persisted:
        return _format_events(buffered)

    last_ts = persisted[-1].get("ts") or 0
    tail = [e for e in buffered if (e.get("ts") or 0) > last_ts]
    return _format_events(persisted + tail)


# --------------------------------------------------------------------------- #
# Phase 4a: SSE streaming endpoint for live task events                        #
# --------------------------------------------------------------------------- #

def _resolve_task_id(sched, prefix: str) -> str | None:
    """Resolve a short task-id prefix to the full id in the event log."""
    for tid in list(sched._event_log.keys()):
        if tid.startswith(prefix):
            return tid
    return None


@app.get("/api/tasks/{task_id}/events/stream")
async def task_events_stream(task_id: str, request: Request):
    """SSE endpoint — streams task events as they arrive.

    The client opens an EventSource to this URL. Each SSE frame is a JSON
    object with {ts, kind, text, source}. The stream closes when the task
    leaves the inflight set and no more events arrive for 5 s.
    """
    sched = getattr(request.app.state, "scheduler", None)
    if sched is None:
        return PlainTextResponse("no scheduler", status_code=503)

    full_id = _resolve_task_id(sched, task_id)

    # W2.3: each frame carries `id: <ts>`, so the browser's NATIVE EventSource
    # reconnect resumes from where it dropped (Last-Event-ID header) instead
    # of replaying the whole deque or — worse — the client giving up. The
    # client no longer closes on transient errors.
    last_event_id = request.headers.get("last-event-id", "")

    async def _generate():
        nonlocal full_id
        try:
            last_ts = float(last_event_id)  # resume-from cursor on reconnect
        except (TypeError, ValueError):
            last_ts = 0.0  # timestamp-based cursor (deque rotates at maxlen=200)
        idle_ticks = 0
        while True:
            # Resolve lazily — task may start after SSE connection opens.
            if full_id is None:
                full_id = _resolve_task_id(sched, task_id)
                if full_id is None:
                    await asyncio.sleep(1)
                    idle_ticks += 1
                    if idle_ticks > 30:  # give up after 30 s
                        yield "data: {\"kind\": \"done\", \"text\": \"task not found\"}\n\n"
                        return
                    continue
                idle_ticks = 0  # resolved — reset so done-detection starts fresh

            events = sched.task_events(full_id)
            new_events = [e for e in events if (e.get("ts") or 0) > last_ts]
            for e in new_events:
                source = e.get("source", "")
                kind = e.get("kind", "")
                text = ""
                # watcher/human pass through like orchestrator — same filter
                # gap as _format_events (post-PR ladder was invisible live).
                if source in ("orchestrator", "watcher", "human") \
                        or kind in ("result", "error"):
                    text = e.get("text", "")
                elif is_agent_session(source) and kind == "tool_use":
                    text = _summarize_tool(e.get("tool_name", ""), e.get("tool_input") or {})
                    kind = "tool_use"
                elif is_agent_session(source) and kind == "text" and (e.get("text") or "").strip():
                    text = (e.get("text") or "").strip()[:600]
                    kind = "agent_text"
                elif is_agent_session(source) and kind.startswith("subagent_"):
                    text = (e.get("text") or "").strip()[:300]
                else:
                    continue
                frame_data = {"ts": e.get("ts"), "kind": kind,
                              "text": text, "source": source}
                if kind == "models" and isinstance(e.get("models"), dict):
                    frame_data["models"] = e["models"]
                if isinstance(e.get("message"), str) and e["message"]:
                    frame_data["message"] = e["message"]
                if kind.startswith("subagent_"):
                    for key in ("task_id", "task_type", "status"):
                        if key in e:
                            frame_data[key] = e[key]
                frame = json.dumps(frame_data)
                yield f"id: {e.get('ts') or 0}\ndata: {frame}\n\n"
            if new_events:
                last_ts = max(e.get("ts") or 0 for e in new_events)

            # Check if task is done (not inflight and no new events for 5 ticks).
            if full_id not in sched.inflight:
                idle_ticks += 1
                if idle_ticks > 5:
                    yield "data: {\"kind\": \"done\", \"text\": \"stream ended\"}\n\n"
                    return
            else:
                idle_ticks = 0

            # Wait for new events or timeout.
            notify = sched._event_notify.get(full_id)
            if notify is not None:
                notify.clear()
                try:
                    await asyncio.wait_for(notify.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(1)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/worker/status")
async def worker_status(request: Request) -> dict[str, Any]:
    """Is the embedded worker running? How many tasks in-flight?"""
    sched = getattr(request.app.state, "scheduler", None)
    watcher_error = getattr(request.app.state, "watcher_error", None)
    if sched is None:
        return {"running": False, "inflight": 0, "max_workers": 0,
                "watcher_error": watcher_error}
    return {
        "running": True,
        "inflight": len(sched.inflight),
        "max_workers": sched.max_workers,
        "watcher_error": watcher_error,
    }


@app.get("/api/queue/health")
async def queue_health_endpoint(request: Request) -> dict[str, Any]:
    """D2 #4: is the queue stuck, and when does it drain? Pure timestamps."""
    from ..core.health import queue_health
    store: Store = request.app.state.store
    h = await queue_health(store)
    return h.as_dict()


@app.get("/api/metrics")
async def metrics(request: Request) -> dict[str, Any]:
    """The north-star numbers (M4): PRs opened/merged, attempts and tokens
    per PR, burn per auth profile, gate outcomes, repro-gate verdict split.
    Read-only SQL over the record — nothing derived, nothing cached."""
    from ..core.metrics import compute_metrics, playbook_outcomes
    data = await compute_metrics(_store(request))
    # D2 #5: which playbooks actually pay (gate rate + burn).
    data["by_playbook"] = await playbook_outcomes(request.app.state.store)
    return data


@app.get("/api/autonomy")
async def autonomy_report(request: Request, days: int | None = None) -> dict[str, Any]:
    """Autonomy telemetry (megaplan P0): mid-flight-touchpoint rate vs.
    PR-reached rate. Read-only."""
    from ..core.autonomy import compute_autonomy_metrics
    rep = await compute_autonomy_metrics(_store(request), days=days)
    return rep.as_dict()


@app.post("/api/tasks/{task_id}/reply")
async def reply_task(
    task_id: str, body: ReplyRequest, request: Request
) -> dict[str, Any]:
    """Store a human answer to a parked task's question; reset to IMPLEMENTING.

    Does NOT auto-run the orchestrator — the human runs `nh watch <id>` to resume.
    Parity with `nh reply --no-run`.
    """
    from ..blockers import (
        ActionError,
        Blocker,
        apply_action,
        is_terminal_action,
        resume_checkpoint,
    )

    store = _store(request)
    task = await _require_task(store, task_id)
    if task.status not in _PARKED_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"task is {task.status.value!r}, not a parked state — no question to answer",
        )
    ctx = task.context or {}
    replies = ctx.get("human_replies") or []
    blocker = task.blocker or {}
    question = blocker.get("question") if isinstance(blocker, dict) else None

    # Picking an option is the only path that applies its action, and it runs
    # only here, on a human's click.
    answer, applied, terminal = body.answer, None, False
    if body.choose is not None:
        options = Blocker.from_dict(blocker).options if blocker else []
        if not 1 <= body.choose <= len(options):
            raise HTTPException(
                status_code=400, detail=f"choose must be between 1 and {len(options)}",
            )
        option = options[body.choose - 1]
        answer = option.label
        terminal = is_terminal_action(option.action)
        try:
            applied = apply_action(task, option.action)
        except ActionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    await store.append_context_list(task.id, "human_replies", {
        "at": _now(), "question": question, "answer": answer,
        "applied": applied,
    })
    # Terminal option (SCRUM-22): the human chose "stop — keep parked". Record
    # the answer and leave the parked status untouched; resuming here is what
    # silently inverted the stop.
    if terminal:
        await store.update_task_columns(task)
        return {"ok": True, "status": task.status.value, "kept_parked": True}
    patch: dict[str, Any] = {"wake_check_at": None}
    # Continue from the [WIP-BLOCKED] checkpoint rather than from base.
    checkpoint = resume_checkpoint(blocker)
    if checkpoint:
        patch["resume_from"] = checkpoint
    task.context = await store.merge_context(task.id, patch)
    task.wake_check_at = None
    await store.update_task_columns(task)
    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({
        "type": "task_updated",
        "task_id": task.id,
        "status": TaskStatus.IMPLEMENTING.value,
        "tasks": [t.model_dump() for t in tasks],
    })
    return {
        "ok": True,
        "message": f"Reply stored. Run `nh watch {task_id[:8]}` to resume.",
    }


# --------------------------------------------------------------------------- #
# Knowledge management: rules, skills, learnings, config                      #
# --------------------------------------------------------------------------- #


class _MemoryBody(BaseModel):
    title: str
    content: str
    tags: list[str] = []
    project: str | None = None


@app.get("/api/rules")
async def list_rules(request: Request) -> list[dict[str, Any]]:
    store = _store(request)
    from ..learning import TYPE_RULE, TYPE_ANTI_PATTERN
    items = await store.list_memories(confirmed=True, mem_type=TYPE_RULE)
    items += await store.list_memories(confirmed=True, mem_type=TYPE_ANTI_PATTERN)
    return [dict(r) for r in items]


@app.post("/api/rules", status_code=201)
async def add_rule(body: _MemoryBody, request: Request) -> dict[str, Any]:
    store = _store(request)
    from ..learning import TYPE_RULE
    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title=body.title, content=body.content,
        tags=body.tags, project=body.project,
        source="board", confirmed=True,
    )
    if not mem_id:
        raise HTTPException(status_code=409, detail="Duplicate rule")
    return {"ok": True, "id": mem_id, "title": body.title}


@app.delete("/api/rules/{rule_id}")
async def remove_rule(rule_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    m = await store.find_memory(rule_id)
    if not m:
        raise HTTPException(status_code=404, detail=f"rule {rule_id!r} not found")
    await store.delete_memory(m["id"])
    return {"ok": True, "id": m["id"]}


@app.get("/api/skills")
async def list_skills(request: Request) -> list[dict[str, Any]]:
    store = _store(request)
    from ..learning import TYPE_SKILL, TYPE_FACT
    items = await store.list_memories(confirmed=True, mem_type=TYPE_SKILL)
    items += await store.list_memories(confirmed=True, mem_type=TYPE_FACT)
    return [dict(r) for r in items]


@app.post("/api/skills", status_code=201)
async def add_skill(body: _MemoryBody, request: Request) -> dict[str, Any]:
    store = _store(request)
    from ..learning import TYPE_SKILL
    mem_id = await store.add_memory(
        mem_type=TYPE_SKILL, title=body.title, content=body.content,
        tags=body.tags, project=body.project,
        source="board", confirmed=True,
    )
    if not mem_id:
        raise HTTPException(status_code=409, detail="Duplicate skill")
    return {"ok": True, "id": mem_id, "title": body.title}


@app.delete("/api/skills/{skill_id}")
async def remove_skill(skill_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    m = await store.find_memory(skill_id)
    if not m:
        raise HTTPException(status_code=404, detail=f"skill {skill_id!r} not found")
    await store.delete_memory(m["id"])
    return {"ok": True, "id": m["id"]}


@app.get("/api/learnings")
async def list_learnings(
    request: Request, active: bool = False,
) -> list[dict[str, Any]]:
    store = _store(request)
    from ..learning import LearningQueue
    q = LearningQueue(store)
    rows = await (q.active() if active else q.pending())
    return [dict(r) for r in rows]


@app.post("/api/learnings/{mem_id}/confirm")
async def confirm_learning(mem_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    m = await store.find_memory(mem_id)
    if not m:
        raise HTTPException(status_code=404, detail=f"proposal {mem_id!r} not found")
    from ..learning import LearningQueue
    await LearningQueue(store).confirm(m["id"])
    return {"ok": True, "id": m["id"]}


@app.post("/api/learnings/{mem_id}/reject")
async def reject_learning(mem_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    m = await store.find_memory(mem_id)
    if not m:
        raise HTTPException(status_code=404, detail=f"proposal {mem_id!r} not found")
    from ..learning import LearningQueue
    await LearningQueue(store).reject(m["id"])
    return {"ok": True, "id": m["id"]}


_SECRET_KEY_RE = re.compile(r"(token|secret|password|webhook|key)", re.IGNORECASE)


def _scrub_secrets(value: Any) -> Any:
    """Recursively replace secret-shaped string values with a marker.

    Any dict key matching `_SECRET_KEY_RE` whose value is a non-empty string
    is replaced with "●●● set". Empty/None values pass through unchanged.
    Operates on (and returns) a fresh structure — callers must pass a
    deep copy so the running config is never mutated.
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if (
                isinstance(k, str)
                and _SECRET_KEY_RE.search(k)
                and isinstance(v, str)
                and v
            ):
                out[k] = "●●● set"
            else:
                out[k] = _scrub_secrets(v)
        return out
    if isinstance(value, list):
        return [_scrub_secrets(item) for item in value]
    return value


@app.get("/api/config")
async def show_config(request: Request) -> dict[str, Any]:
    """Return the current config (safe subset — no secrets)."""
    cfg = request.app.state.config
    data = copy.deepcopy(cfg.data)
    return _scrub_secrets(data)


@app.get("/api/integrations")
async def list_integrations_endpoint(request: Request) -> dict[str, Any]:
    """Status of every integration (configured + kind; healthy is null until a
    `test` is run), PLUS its `fields` array so the UI can render a settings
    form. Never returns a secret — `fields` carries only `set: bool`."""
    from ..integrations import integration_fields, list_integrations
    cfg = request.app.state.config
    out = []
    for s in list_integrations(cfg.data):
        d = asdict(s)
        d["fields"] = integration_fields(s.name, cfg.data)
        out.append(d)
    return {"integrations": out}


@app.post("/api/integrations/{name}/test")
async def test_integration_endpoint(name: str, request: Request) -> dict[str, Any]:
    """Run a live health check for one integration. The returned `detail` is a
    human-readable message that never contains a token or secret."""
    from ..config import AuthError, load_env_var
    from ..integrations import (
        FIELD_SPECS,
        test_integration as run_integration_test,
    )

    # Load this integration's secret(s) from ~/.no_human/.env into the process
    # env BEFORE the health check. Without this the button could only
    # authenticate when the server happened to be started via `nh serve` (whose
    # Jira poll loads JIRA_API_TOKEN) — from a plain `nh start`, the token was
    # absent and every "Test connection" reported it unset. The .env stays the
    # only source of the secret; config comes from app.state like every other
    # endpoint (updated by the save endpoint / a restart), keeping test
    # isolation intact.
    cfg = request.app.state.config
    for spec in FIELD_SPECS.get(name, []):
        if spec.env_var:
            try:
                load_env_var(spec.env_var)
            except AuthError:
                # A metered-auth var is never an integration secret; skip it
                # rather than 500 the whole test.
                pass
    try:
        status = await run_integration_test(name, cfg.data)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return asdict(status)


@app.put("/api/integrations/{name}/config")
async def save_integration_config_endpoint(
    name: str, body: SaveIntegrationConfigRequest, request: Request
) -> dict[str, Any]:
    """Persist one integration's settings-form fields: secrets to
    ``~/.no_human/.env``, everything else to ``config.yaml``. Returns the
    refreshed status card PLUS its `fields` array — NEVER a secret value."""
    from ..config import AuthError
    from ..integrations import KIND_BY_NAME, integration_fields, save_integration_config

    # This route writes ~/.no_human/.env — the SAME credential store the auth
    # endpoint guards. Without this, `allow_origins=["*"]` lets any page the
    # operator visits while `nh serve` is up preflight successfully and then
    # PUT a planted secret into it; that drive-by was demonstrated end to end.
    _require_local_origin(request, writing=True)
    if name not in KIND_BY_NAME:
        raise HTTPException(status_code=404, detail=f"unknown integration: {name!r}")
    try:
        status = save_integration_config(name, body.fields)
    except (ValueError, AuthError) as exc:
        # AuthError is what the shared writer's line guard raises. Uncaught it
        # became a 500 — and because the values are written one key at a time,
        # an earlier key had already landed before a later one was refused.
        raise HTTPException(status_code=422, detail=str(exc))

    # Reload so this response (and subsequent requests) see what was just
    # written — CONFIG_PATH is looked up fresh here too (see integrations'
    # write-path comment), never a stale bound default.
    from ..config import CONFIG_PATH, load_config

    refreshed = load_config(CONFIG_PATH)
    request.app.state.config.data = refreshed.data
    out = asdict(status)
    out["fields"] = integration_fields(name, refreshed.data)
    return out


@app.get("/api/integrations/jira/issues", response_model=list[JiraIssueOut])
async def jira_issues_endpoint(
    q: str = "", limit: int = 20, request: Request = None
) -> list[JiraIssueOut]:
    """Free-text browse/pick over the configured Jira project — the read side
    of Task 1.6's "Import from Jira" affordance. This never creates a task;
    POST /api/tasks (with source="jira") stays the one create path. Reuses
    ``JiraAdapter`` from intake/jira.py completely — same auth, same search
    endpoint, same JIRA_API_TOKEN env var the background poller already uses.
    """
    from ..config import load_env_var
    from ..intake.jira import JiraAdapter

    cfg = request.app.state.config
    # Load JIRA_API_TOKEN from ~/.no_human/.env on demand (B1 pattern). Only the
    # `nh serve` poller loaded it at startup; under `nh start` (the board) it was
    # never in the process env, so the picker wrongly reported "not configured"
    # even with a valid token on file. JiraAdapter reads it from os.environ.
    load_env_var("JIRA_API_TOKEN")
    adapter = JiraAdapter(cfg.data)
    if not adapter.configured:
        raise HTTPException(
            status_code=503,
            detail="Jira is not configured — add it under Settings > Integrations.",
        )
    limit = max(1, min(limit, 50))
    try:
        issues = await asyncio.to_thread(adapter.search_text, q, limit)
    except httpx.HTTPError as exc:
        # Never surface the raw exception (it can carry the request URL/auth
        # object) — a short, tokenless detail only.
        log.warning("jira issue search failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail="Jira search failed — check the site/project configuration.",
        )
    # SCRUM-18 — accidental re-import trap: one local-store read (no per-row
    # Jira calls) building an external_id -> [tasks] index, then attach an
    # `imported` block to any issue that already has a board task. A deleted
    # board task simply isn't in list_tasks() any more, so its ticket goes
    # back to showing no chip — no stale reference is fabricated.
    all_tasks = await _store(request).list_tasks()
    by_ext: dict[str, list] = {}
    for t in all_tasks:
        if t.source == "jira" and t.external_id:
            by_ext.setdefault(t.external_id, []).append(t)
    out = []
    for issue in issues:
        row = JiraIssueOut(**adapter.issue_brief(issue))
        matches = by_ext.get(issue.get("key"))
        if matches:
            latest = max(matches, key=lambda t: t.updated_at)
            row.imported = JiraImportedInfo(
                task_id=latest.id, status=latest.status.value, count=len(matches),
            )
        out.append(row)
    return out


@app.get("/api/integrations/jira/issues/{key}", response_model=JiraIssueOut)
async def jira_issue_detail_endpoint(key: str, request: Request) -> JiraIssueOut:
    """Fetch ONE issue in full (SCRUM-9) — the detail GET behind the picker's
    "pick" action. ``/api/integrations/jira/issues`` (the browse list, above)
    truncates each description to 2000 chars for a small list payload; that
    truncation was leaking into created tasks because the web picker built
    its composer prefill straight from the list brief. This endpoint returns
    the SAME shape with the FULL description, so a picked issue's task can
    carry the whole spec instead of a cut-off one.
    """
    from ..config import load_env_var
    from ..intake.jira import JiraAdapter

    cfg = request.app.state.config
    load_env_var("JIRA_API_TOKEN")
    adapter = JiraAdapter(cfg.data)
    if not adapter.configured:
        raise HTTPException(
            status_code=503,
            detail="Jira is not configured — add it under Settings > Integrations.",
        )
    try:
        issue = await asyncio.to_thread(adapter.get_issue, key)
    except httpx.HTTPError as exc:
        log.warning("jira issue detail fetch failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail="Jira lookup failed — check the site/project configuration.",
        )
    return JiraIssueOut(**adapter.issue_detail(issue))


# --------------------------------------------------------------------------- #
# Onboarding wizard (web first-run). Reuses the existing onboard/history/      #
# learning logic — no parallel machinery. Heavy proving (running a repo's      #
# tests) stays on the `nh onboard` path; here we derive + persist an unproven  #
# profile, consistent with the codebase's deliberate derive/prove split.       #
# --------------------------------------------------------------------------- #

class RepoDetectRequest(BaseModel):
    root: str | None = None  # defaults to ~/git

class RepoOnboardRequest(BaseModel):
    repo_path: str

class HistoryAnalyzeRequest(BaseModel):
    days: int = 30

class ConfirmRulesRequest(BaseModel):
    ids: list[str] = []

class OnboardingCompleteRequest(BaseModel):
    team: str | None = None
    repos: list[str] = []
    docs: list[str] = []


def _read_onboarding(config) -> dict[str, Any]:
    return dict((config.data.get("onboarding") or {}))


def _persist_onboarding(config, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge `patch` into config.onboarding, in memory AND on disk (config.yaml).
    Mirrors how cli/init_cmd writes config — no secrets are touched here."""
    import yaml
    from ..config import CONFIG_PATH
    ob = dict((config.data.get("onboarding") or {}))
    ob.update(patch)
    config.data["onboarding"] = ob
    try:
        on_disk = yaml.safe_load(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    except Exception:  # noqa: BLE001
        on_disk = {}
    on_disk = on_disk or {}
    on_disk["onboarding"] = ob
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(CONFIG_PATH, yaml.safe_dump(on_disk, sort_keys=False))
    except OSError as exc:
        log.warning("could not persist onboarding to %s: %s", CONFIG_PATH, exc)
    return ob


def _find_git_repos(root: Path, *, max_depth: int = 7, limit: int = 500) -> list[Path]:
    """Bounded scan for git repos under `root` (don't descend into a repo, cap
    depth + count so a huge tree can't hang the request)."""
    found: list[Path] = []

    def walk(d: Path, depth: int) -> None:
        if depth > max_depth or len(found) >= limit:
            return
        if (d / ".git").exists():
            found.append(d)
            return  # a repo is a leaf — don't descend
        try:
            entries = sorted(p for p in d.iterdir() if p.is_dir())
        except OSError:
            return
        for e in entries:
            if e.name.startswith("."):
                continue
            walk(e, depth + 1)

    walk(root, 0)
    return found


def _quick_ecosystem(repo: Path) -> str:
    if (repo / "package.json").exists():
        return "node"
    if (repo / "uv.lock").exists() or (repo / "pyproject.toml").exists():
        return "python"
    if (repo / "pom.xml").exists():
        return "maven"
    if (repo / "go.mod").exists():
        return "go"
    return ""


@app.get("/api/fs/suggest")
async def fs_suggest(path: str = "") -> dict[str, Any]:
    """Directory autocomplete for path inputs. Given a partial path, return up to
    20 matching sub-directories (absolute, ~-expanded), flagging git repos. Used
    by the onboarding repo/docs inputs to autofill as the user types."""
    raw = (path or "").strip() or "~"
    expanded = Path(raw).expanduser()
    # If the user is mid-typing a segment (no trailing slash and the path isn't a
    # dir), complete against the parent using the last segment as a prefix.
    if raw.endswith("/") or expanded.is_dir():
        base, prefix = expanded, ""
    else:
        base, prefix = expanded.parent, expanded.name.lower()
    out: list[dict[str, Any]] = []
    try:
        for p in sorted(base.iterdir()):
            if not p.is_dir() or p.name.startswith("."):
                continue
            if prefix and not p.name.lower().startswith(prefix):
                continue
            out.append({"path": str(p), "name": p.name,
                        "is_repo": (p / ".git").exists()})
            if len(out) >= 20:
                break
    except OSError:
        pass
    return {"base": str(base), "suggestions": out}


@app.get("/api/onboarding/status")
async def onboarding_status(request: Request) -> dict[str, Any]:
    ob = _read_onboarding(request.app.state.config)
    return {"completed": bool(ob.get("completed")), **ob}


@app.post("/api/onboarding/repos/detect")
async def onboarding_detect_repos(
    body: RepoDetectRequest, request: Request
) -> dict[str, Any]:
    root = Path(body.root).expanduser() if body.root else Path.home() / "git"
    if not root.is_dir():
        return {"root": str(root), "repos": [], "error": f"{root} is not a directory"}
    repos = await asyncio.to_thread(_find_git_repos, root)
    return {
        "root": str(root),
        "repos": [
            {"path": str(p), "name": p.name, "ecosystem": _quick_ecosystem(p)}
            for p in repos
        ],
    }


@app.post("/api/onboarding/repos/onboard")
async def onboarding_onboard_repo(
    body: RepoOnboardRequest, request: Request
) -> dict[str, Any]:
    """Derive a ProjectProfile from the repo's declarations and persist it
    UNPROVEN. Proving (running the test suite) is intentionally deferred to
    `nh onboard <repo>` so a click here never blocks on a long test run."""
    store = _store(request)
    config = request.app.state.config
    repo = Path(body.repo_path).expanduser().resolve()
    if not repo.is_dir() or not (repo / ".git").exists():
        raise HTTPException(422, f"{body.repo_path!r} is not a git repository")

    from ..onboard import DeclarationDeriver, derive_required_credentials, OnboardEngine
    from ..profile import ProjectProfile

    derived = await asyncio.to_thread(DeclarationDeriver().derive, repo)
    vcs_host, vcs_remote = await asyncio.to_thread(OnboardEngine._derive_vcs, repo)

    def _first(kind: str) -> str:
        cands = derived.of_kind(kind)
        return cands[0].command if cands else ""

    github_hosts = (config.data.get("git") or {}).get("github_hosts") or ["github.com"]
    profile = ProjectProfile(
        repo_path=str(repo),
        ecosystem=derived.ecosystem,
        install_cmd=_first("install"),
        test_cmd=_first("test"),
        lint_cmd=_first("lint"),
        ci=derived.ci,
        human_gated_steps=derived.human_gated_steps,
        vcs_host=vcs_host,
        vcs_remote=vcs_remote,
        required_credentials=derive_required_credentials(
            derived.ci, vcs_host, derived.human_gated_steps, github_hosts),
        derived_from=sorted(set(derived.sources)),
        proven={},          # unproven — prove via `nh onboard <repo>`
        confirmed=False,
        notes="derived in onboarding wizard (unproven — run `nh onboard` to prove)",
    )
    await store.upsert_profile(profile)
    return {
        "ok": True,
        "repo_path": str(repo),
        "ecosystem": profile.ecosystem,
        "install_cmd": profile.install_cmd,
        "test_cmd": profile.test_cmd,
        "lint_cmd": profile.lint_cmd,
        "required_credentials": profile.required_credentials,
        "proven": False,
    }


async def _gather_history(days: int) -> tuple[list, dict[str, int]]:
    """Combine conversation history from every available source: Windsurf  # term-ok: real IDE names, functional
    (best-effort — needs a running IDE) AND Claude Code (read from disk, always
    available). Returns (transcripts, per-source counts)."""
    from ..history.extractor import extract_transcripts, IDENotRunningError
    from ..history.claude_code import extract_claude_code_transcripts

    transcripts: list = []
    sources: dict[str, int] = {}
    try:
        ws = await asyncio.to_thread(extract_transcripts, days=days)
        transcripts += ws
        sources["windsurf"] = len(ws)  # term-ok: internal source tag names the real IDE
    except IDENotRunningError:
        sources["windsurf"] = 0  # term-ok: internal source tag names the real IDE
    except Exception as exc:  # noqa: BLE001
        log.warning("Windsurf extract failed: %s", exc)  # term-ok: real IDE name
        sources["windsurf"] = 0  # term-ok: internal source tag names the real IDE
    try:
        cc = await asyncio.to_thread(extract_claude_code_transcripts, days=days)
        transcripts += cc
        sources["claude_code"] = len(cc)
    except Exception as exc:  # noqa: BLE001
        log.warning("Claude Code extract failed: %s", exc)
        sources["claude_code"] = 0
    return transcripts, sources


@app.post("/api/onboarding/history/extract")
async def onboarding_history_extract(request: Request) -> dict[str, Any]:
    """Count extractable transcripts across all sources (Windsurf + Claude  # term-ok: real IDE names
    Code) and the user's skills. Honest when a source is empty (no fake data)."""
    from ..history.skills import discover_skills
    transcripts, sources = await _gather_history(30)
    skills = await asyncio.to_thread(discover_skills)
    return {
        "available": bool(transcripts) or bool(skills),
        "transcripts": len(transcripts),
        "messages": sum(len(t.messages) for t in transcripts),
        "sources": sources,
        "skills": len(skills),
        "detail": "no Windsurf IDE and no Claude Code history found"  # term-ok: real IDE name (user-facing)
                  if not transcripts else "",
    }


@app.post("/api/onboarding/history/analyze")
async def onboarding_history_analyze(
    body: HistoryAnalyzeRequest, request: Request
) -> dict[str, Any]:
    """Extract transcripts → analyze for corrections → propose each into the
    human-confirmed learning queue (confirmed=0). Nothing becomes an active rule
    until confirmed — preserving the learning-queue invariant."""
    store = _store(request)
    from ..history.ingester import TranscriptIngester
    transcripts, sources = await _gather_history(body.days)
    messages = sum(len(t.messages) for t in transcripts)

    # Build an LLM-distillation pass so proposed rules are GENERAL, durable
    # lessons (importance-labelled) rather than raw matched user messages — and
    # one-off task requests get filtered out. Uses the cheaper review model at
    # low effort, read-only. If the backend/auth is unavailable the ingester
    # falls back to the heuristic pass (still works), so this never hard-fails.
    config = request.app.state.config
    llm_call = None
    try:
        from ..agent.claude_backend import ClaudeBackend
        _b = ClaudeBackend(model=config.review_model, readonly=True)

        async def llm_call(prompt: str) -> str:  # noqa: F811
            res = await _b.run(prompt, cwd=Path.cwd(), max_turns=1, effort="low")
            return res.final_text or ""
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM analyzer unavailable, heuristic-only: %s", exc)

    # Route through the standalone ingester (EVOLUTION_PLAN §1.1) so the web
    # wizard, the CLI, and periodic re-analysis all share one code path. It
    # enqueues every finding as source="proposed"/confirmed=0 with a stable
    # dedupe_key (idempotent) — nothing activates until a human confirms it.
    ingester = TranscriptIngester(store, llm_call=llm_call)
    result = await ingester.ingest_transcripts(transcripts, use_llm=llm_call is not None)
    proposals = list(result.proposals)

    # Also catalog the user's Claude Code skills as proposed `skill` memories —
    # so the rules-review shows them and (once confirmed) the Supervisor's
    # "skill-exists" detector knows they exist (EVOLUTION_PLAN §1.3 row 1).
    from ..history.skills import discover_skills
    skills_added = 0
    for s in await asyncio.to_thread(discover_skills):
        mid = await store.add_memory(
            mem_type="skill", title=s.name, content=s.description or s.name,
            tags=["skill", "claude_code"], source="proposed", confirmed=False,
            dedupe_key=f"skill:{s.name}",
        )
        if mid:
            skills_added += 1
            proposals.append({"id": mid, "category": "skill", "title": s.name,
                              "content": s.description or s.name, "importance": "med"})

    return {"available": True, "proposed": result.proposed + skills_added,
            "duplicates": result.duplicates, "messages": messages,
            "sources": sources, "skills": skills_added,
            "transcripts": result.transcripts, "proposals": proposals}


@app.post("/api/onboarding/rules/confirm")
async def onboarding_confirm_rules(
    body: ConfirmRulesRequest, request: Request
) -> dict[str, Any]:
    """Confirm selected proposed learnings → they become active rules. Reuses
    the existing LearningQueue.confirm (the only path that activates a rule)."""
    store = _store(request)
    from ..learning import LearningQueue
    q = LearningQueue(store)
    confirmed = 0
    for mem_id in body.ids:
        m = await store.find_memory(mem_id)
        if m and await q.confirm(m["id"]):
            confirmed += 1
    return {"ok": True, "confirmed": confirmed}


@app.post("/api/onboarding/complete")
async def onboarding_complete(
    body: OnboardingCompleteRequest, request: Request
) -> dict[str, Any]:
    config = request.app.state.config
    ob = _persist_onboarding(config, {
        "completed": True,
        "completed_at": _now(),
        "team": body.team,
        "repos": body.repos,
        "docs": body.docs,
    })
    return {"ok": True, "onboarding": ob}


class DocsGenerateRequest(BaseModel):
    repo_path: str


@app.post("/api/onboarding/docs/generate")
async def onboarding_docs_generate(
    body: DocsGenerateRequest, request: Request
) -> dict[str, Any]:
    """Generate wiki docs for a repo via a bounded Agent SDK session."""
    from ..docs_gen import WikiGenerator
    from ..agent.claude_backend import ClaudeBackend

    config = request.app.state.config
    backend = ClaudeBackend(
        model=config.primary_model,
        forbidden_paths=config["safety"]["forbidden_paths"],
    )
    gen = WikiGenerator(backend, max_turns=12)
    result = await gen.generate(body.repo_path)
    if result.error:
        return {"ok": False, "error": result.error}

    # Persist wiki_commit to the profile if one exists.
    from ..profile import ProjectProfile
    profile = ProjectProfile.load(body.repo_path)
    if profile and result.commit_sha:
        profile.wiki_commit = result.commit_sha
        profile.save()

    return {
        "ok": True,
        "files": result.files_written,
        "commit_sha": result.commit_sha,
    }


# --------------------------------------------------------------------------- #
# WebSocket — live board (polls DB every 2 s, broadcasts on change)           #
# --------------------------------------------------------------------------- #

def _task_fingerprint(tlist) -> dict:
    """B2 #10: the old (status, updated_at, live_status) tuple missed card
    fields (subtask_progress, pr_url, attempt_count, cancelled) — a subtask
    completing bumps only the CHILD row, so the parent card stayed stale
    forever (full-snapshot pushes can't repair what never sends). Hash the
    whole summary payload instead."""
    return {t.id: hash(json.dumps(t.model_dump(), sort_keys=True, default=str))
            for t in tlist}


@app.websocket("/ws")
async def ws_board(ws: WebSocket) -> None:
    await _mgr.connect(ws)
    store: Store = ws.app.state.store
    sched = getattr(ws.app.state, "scheduler", None)
    try:
        # Initial snapshot.
        tasks = await _board_tasks(store, scheduler=sched)
        await _mgr.send(ws, json.dumps({
            "type": "init",
            "tasks": [t.model_dump() for t in tasks],
        }))
        # Sync loop: any change in the FULL summary payload pushes (B2 #10).
        # The client never sends, so a completed receive() means DISCONNECT —
        # awaited alongside the poll pause, or an idle board (nothing to send,
        # nothing to raise) leaked this task polling the store every 2s per
        # closed tab, forever (PR #109 review, proven empirically).
        prev_fp = _task_fingerprint(tasks)
        recv = asyncio.ensure_future(ws.receive())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {recv}, timeout=2, return_when=asyncio.FIRST_COMPLETED)
                if recv in done:
                    return  # client closed (or spoke) — the finally cleans up
                tasks = await _board_tasks(store, scheduler=sched)
                curr_fp = _task_fingerprint(tasks)
                if curr_fp != prev_fp:
                    sched = getattr(ws.app.state, "scheduler", None)
                    worker = {"inflight": len(sched.inflight) if sched else 0}
                    await _mgr.send(ws, json.dumps({
                        "type": "sync",
                        "tasks": [t.model_dump() for t in tasks],
                        "worker": worker,
                    }))
                    prev_fp = curr_fp
        finally:
            recv.cancel()
            # The normal-disconnect RETURN path skipped _mgr.remove, so closed
            # tabs accumulated inert socket+lock entries until the next
            # broadcast pruned them (PR #109 round-2, low). remove() is
            # idempotent — the except paths below stay correct.
            _mgr.remove(ws)
    except WebSocketDisconnect:
        _mgr.remove(ws)
    except Exception:  # noqa: BLE001
        # B2 #9: remove-without-close left the CLIENT's onclose unfired — the
        # board froze while still showing "Connected". Close so it reconnects.
        _mgr.remove(ws)
        with contextlib.suppress(Exception):
            await ws.close()


# --------------------------------------------------------------------------- #
# Serve the React SPA (if built)                                               #
# --------------------------------------------------------------------------- #

if _WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(_WEB_DIST / "assets")), name="assets")

    @app.get("/", include_in_schema=False)
    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str = "") -> FileResponse:
        # Never intercept /api/ or /ws paths — those are backend routes.
        # If they reach here, it means the route doesn't exist (404).
        if path.startswith("api/") or path.startswith("ws"):
            return PlainTextResponse(f"Not found: /{path}", status_code=404)
        # no-cache: index.html references content-hashed assets; without an
        # explicit header Chromium's HEURISTIC freshness serves a stale app
        # shell after every deploy (found live: the Electron shell ran a
        # bundle two deploys old while the e2e gate — which spins its own
        # static server — stayed green). Hashed /assets remain long-cacheable.
        return FileResponse(str(_WEB_DIST / "index.html"),
                            headers={"Cache-Control": "no-cache"})
