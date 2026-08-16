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
import threading
import time
from dataclasses import asdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlsplit

if TYPE_CHECKING:  # import-cycle-free: the eval package is loaded lazily below
    from ..eval.northstar_card import NorthStarCard

import httpx
from fastapi import (
    FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from starlette.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import _atomic_write_text, load_config
from ..core.db import Store
from ..core.lanes import lane_for
from ..core.orchestrator import Orchestrator, is_agent_session, is_narration
from ..core.task import Task, TaskStatus
from ..vcs.task_pr import task_has_pr_evidence
from .models import (
    AttemptOut, BoardPayload, CancelRequest, CreateProjectRequest, CreateTaskRequest,
    GrillQuestionOut, GrillResultOut, GrillStepRequest, IntegrationSetupRequest,
    ImportedInfo, LandedOverrideRequest, ProjectOut, ReplyRequest,
    SaveIntegrationConfigRequest, SendBackRequest, ShippedRequest, TaskOut,
    TaskSummaryOut, TelemetryConsentRequest, TrackerIssueOut, UpdateProjectRequest,
)

import logging

log = logging.getLogger("no_human.api")

# Read the platform through a constant, never an inline `os.name` test, so the
# Windows branches below are reachable from a test on any host. No Windows
# machine or runner is available to this project.
_IS_WINDOWS = os.name == "nt"

def _resolve_web_dist() -> Path:
    """Locate the built React board across the three ways this code ships.

    There is no single path that works for all three, so each is tried in turn:

    1. **Repo checkout / frozen desktop bundle** — ``parents[3]/web/dist``.
       In a checkout ``__file__`` is ``<repo>/src/no_human/api/app.py``, so
       parents[3] is the repo root. Under a PyInstaller onedir freeze it is
       ``<bundle>/_internal/no_human/api/app.py``, so parents[3] is the bundle
       root, which is where ``packaging/build-installer.sh`` copies the board.
       Both land on ``web/dist`` with no change to this line — that equivalence
       is deliberate and ``packaging/nh-server.spec`` depends on it.
    2. **Wheel install** — ``<site-packages>/no_human/web_dist``. parents[3] is
       meaningless there (it points at ``lib/python3.X``, outside the package),
       so the board is shipped INSIDE the package instead. ``pyproject.toml``
       force-includes ``web/dist`` to that name at build time.

    Returning the first candidate that exists means a repo checkout never sees
    a stale wheel-style copy and vice versa: at most one of these ever exists.
    The first candidate is returned as the fallback when neither is present, so
    the "board was never built" message names the path a developer expects.
    """
    candidates = (
        Path(__file__).resolve().parents[3] / "web" / "dist",  # checkout / frozen
        Path(__file__).resolve().parent.parent / "web_dist",   # installed wheel
    )
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return candidates[0]


_WEB_DIST = _resolve_web_dist()


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    # Pin the loaded-code snapshot HERE, before anything can run, so it is the
    # sha of what this process holds in memory rather than of whatever HEAD
    # happens to be the first time something asks. The server never reloads:
    # every attempt this process records will carry this value.
    # Off the event loop: this is four git subprocesses (~294ms measured, and
    # 40s in the worst case the timeouts allow). Startup is exactly when the
    # loop has other things to do.
    from ..core.build_info import loaded_code, staleness_note
    code = await asyncio.to_thread(loaded_code)
    app.state.loaded_code = code.descriptor
    log.info("loaded code: %s", code.descriptor)
    # WARNING level on purpose: uvicorn runs at log_level="warning", so INFO is
    # dropped and only the line that has something to say survives. Advisory —
    # nothing below reads it, and no task is prevented from being claimed.
    # This line alone is NOT the surface: it scrolls past at boot, and the case
    # that matters is a server that has been up for hours. See the board banner
    # fed by /api/worker/status.
    _startup_stale = await asyncio.to_thread(staleness_note, code)
    if _startup_stale:
        log.warning("%s", _startup_stale)
    # `nh start` may already have connected a shared Store to hand to its
    # Jira/Linear intake pollers (started before uvicorn's ASGI lifespan
    # fires) — reuse it instead of opening a SECOND aiosqlite connection to
    # the same file. Two connections racing this lifespan's own connect+
    # migrate, with no busy_timeout set, is what flooded a clean `nh start`
    # with `sqlite3.OperationalError: database is locked` (KI, 2026-08-01).
    # One-shot handoff (popped, not just read) so a later lifespan cycle in
    # the same process never reuses an already-closed store.
    external_store = getattr(app.state, "_external_store", None)
    if external_store is not None:
        del app.state._external_store
    store = external_store or await Store(config.db_path).connect()
    app.state.store = store
    app.state.config = config
    # CSP is computed once per app start from the loaded config: strict by
    # default, widened by exactly the PostHog hosts when the operator opted in.
    app.state.csp = _build_csp(config.data)
    # Opt-in telemetry (default OFF — record() no-ops without consent).
    try:
        from .. import telemetry as _telemetry
        _telemetry.record("app_started", config=config.data)
    except Exception:
        pass

    # Always start the embedded worker — board up = worker up.
    # CLI may override max_workers/poll_interval via app.state._worker_opts.
    from ..core.runtime import build_orchestrator
    from ..core.scheduler import Scheduler, resolve_max_workers

    def _orch_factory(task=None):
        # ONE construction site for CLI and server alike (core/runtime).
        # The server used to hardcode ClaudeBackend here, so a task run
        # through the GUI ignored `worker.backend` while the same task via
        # `nh` honoured it (audit A8/X2, 2026-08-11).
        return build_orchestrator(config, store, task=task)

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
            branch_landed_commit, check_pr_comments, default_ci_annotations,
            default_ci_log_excerpt, default_pr_checks, default_pr_merged,
            default_pr_mergeable, default_pr_state,
        )
        watcher = WakeWatcher(
            store, config.data,
            pr_merged=default_pr_merged, pr_comment=check_pr_comments,
            pr_state=default_pr_state, pr_checks=default_pr_checks,
            pr_mergeable=default_pr_mergeable,
            ci_log=default_ci_log_excerpt,
            ci_annotations=default_ci_annotations,
            pr_shipped=branch_landed_commit,
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

    # Memory lifecycle C: the daily unconfirmed-proposal sweep (AC1).
    # `enabled: False` is honoured by passing None instead of constructing
    # the job — same shape as `reanalysis` above. Numeric coercions are
    # wrapped: a malformed config.yaml value here (`float("abc")`) must not
    # take the whole lifespan/board down with it.
    retirement_job = None
    learning_cfg = config.data.get("learning", {})
    if learning_cfg.get("sweep_enabled", True):
        try:
            from ..core.scheduler import RetirementSweepJob
            retirement_job = RetirementSweepJob(
                store,
                interval_seconds=float(
                    learning_cfg.get("sweep_interval_seconds", 86400)),
                archive_after_days=int(
                    learning_cfg.get("archive_unconfirmed_days", 45)),
            )
        except (TypeError, ValueError) as exc:
            log.error("bad learning.* sweep config — retirement sweep "
                      "disabled this run: %s", exc)
            retirement_job = None

    sched = Scheduler(
        store, _orch_factory,
        max_workers=max_workers,
        wake_watcher=watcher,
        on_event=lambda k, t: log.info("worker: %s — %s", k, t),
        reanalysis_job=reanalysis,
        retirement_job=retirement_job,
        config=config.data,
    )
    stop_event = asyncio.Event()
    worker_task = asyncio.create_task(
        sched.run_forever(stop=stop_event, poll_interval=poll_interval)
    )

    def _worker_died(task: "asyncio.Task") -> None:
        """Record the worker loop's death where a human can see it.

        WITHOUT THIS THE SERVER LIES FOREVER. `create_task` returns a task
        nobody awaits until shutdown, so an exception inside `run_forever` is
        never retrieved: the coroutine stops, `app.state.scheduler` keeps
        answering, and `/api/worker/status` reports `running: true` for the rest
        of the process's life. asyncio's only complaint is a
        "Task exception was never retrieved" line at garbage-collection time.

        This is not hypothetical, and it is the incident's own mechanism one
        step earlier: `run_forever` awaits `_recover_orphans()` BEFORE its loop
        with no try/except, and that method issues unguarded writes. A
        connection wedged at startup — the inferred first cause of the very
        wedge this release detects — fails those writes with `database is
        locked` and kills the loop before its first tick.

        The endpoint reads `worker_error` and drops `healthy`, exactly as it
        already does for `watcher_error`.
        """
        if task.cancelled():
            return                       # ordinary shutdown, not a failure
        exc = task.exception()
        if exc is None:
            # Returned early without raising. Still fatal — nothing ticks again
            # — and still silent, so it is reported too.
            app.state.worker_error = (
                "the worker loop exited on its own without an error; no task "
                "will be dispatched until the server is restarted")
            log.error("%s", app.state.worker_error)
            return
        app.state.worker_error = f"{type(exc).__name__}: {exc}"
        log.error("THE WORKER LOOP DIED — no task will be dispatched until the "
                  "server is restarted: %s", exc, exc_info=exc)

    # Cleared BEFORE the callback is registered, not after. Safe either way
    # today only because no `await` separates the two lines, so the callback
    # cannot run in between; insert one and a death recorded by `_worker_died`
    # would be silently wiped back to None.
    app.state.worker_error = None
    worker_task.add_done_callback(_worker_died)
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
    # An externally-supplied store is owned by whoever connected it (`nh
    # start`'s `_go()`) — it closes it, not us, or `start()`'s own use of the
    # connection after `server.serve()` returns would hit a closed store.
    if external_store is None:
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

# The two PostHog hosts the browser needs when (and ONLY when) the operator
# has opted in to Usage insights: the recorder/client bundle is fetched from
# us-assets, events + replay payloads POST to both. Exact hosts, nothing wider.
_POSTHOG_SCRIPT_HOST = "https://us-assets.i.posthog.com"
_POSTHOG_CONNECT_HOSTS = "https://us.i.posthog.com https://us-assets.i.posthog.com"


def _build_csp(config_data: dict) -> str:
    """The CSP header value for this app start. With telemetry off (the
    default) this returns `_CSP` UNCHANGED — byte-identical, a test pins it.
    With telemetry configured+enabled, script-src/connect-src gain exactly
    the PostHog hosts above."""
    tel = (config_data or {}).get("telemetry") or {}
    if not (tel.get("enabled") and str(tel.get("posthog_publishable") or "").strip()):
        return _CSP
    return (_CSP
            .replace("script-src 'self'", f"script-src 'self' {_POSTHOG_SCRIPT_HOST}")
            .replace("connect-src 'self' ws: wss:",
                     f"connect-src 'self' ws: wss: {_POSTHOG_CONNECT_HOSTS}"))


@app.middleware("http")
async def _csp_header(request, call_next):
    response = await call_next(request)
    # Computed per app start (lifespan). Fallback: the strict no-telemetry value.
    csp = getattr(request.app.state, "csp", None) or _CSP
    response.headers.setdefault("Content-Security-Policy", csp)
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


async def _emit_task_event(
    store: Store, task_id: str, kind: str, text: str, *, persist: bool = True,
) -> None:
    """Broadcast a merge-progress frame over the existing WebSocket, so the
    SlideOver's live-progress panel sees `merge_started`/`merge_step_*`
    within one server round-trip of the step actually happening.

    ``persist=False`` is for `human_merged`, which `set_status` already
    writes to `task_events` — broadcasting it again here (for a second
    observer/tab watching mid-merge) must not double-insert it."""
    ev = {"source": "human", "kind": kind, "text": text, "ts": time.time()}
    if persist:
        await store.save_events(task_id, [ev])
    await _mgr.broadcast({"type": "task_event", "task_id": task_id, "event": ev})


def _latest_pr_url(attempts: list[dict]) -> str | None:
    for a in reversed(attempts):
        if a.get("pr_url"):
            return a["pr_url"]
    return None


def _max_pr_conflict_rounds() -> int:
    """The configured wake-watcher bound (wake.py reads the same key with the
    same default), surfaced on summaries so the badge renders 'round N/M'."""
    cfg = getattr(app.state, "config", None)
    blockers = cfg.data.get("blockers") if cfg is not None else None
    if not isinstance(blockers, dict):
        # A malformed `blockers:` scalar in config.yaml must degrade to the
        # default, never AttributeError every board endpoint.
        blockers = {}
    try:
        return int(blockers.get("max_pr_conflict_rounds", 3))
    except (TypeError, ValueError):
        return 3


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
            max_pr_conflict_rounds=_max_pr_conflict_rounds(),
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
        # Lane last: it is decided from the fields above, and it is decided HERE
        # rather than in the frontend so every client reads one answer.
        summary.lane = lane_for(summary)
        out.append(summary)
    return out


def _git_diff(repo_path: str, commit_sha: str, base: str | None = None) -> str:
    try:
        diff_range = f"{commit_sha}~1..{commit_sha}"
        if base:
            # A recorded base can still be gone (branch deleted, worktree
            # pruned, base only ever existed on the remote) — verify it
            # resolves before trusting it, so a broken base fails soft into
            # the single-commit range instead of an empty diff.
            check = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"],
                cwd=repo_path, capture_output=True, text=True, timeout=10,
            )
            if check.returncode == 0:
                # Three-dot form diffs from the merge-base, so base moving on
                # after the branch was cut does not inject unrelated files.
                diff_range = f"{base}...{commit_sha}"
            else:
                log.info(
                    "commit_sha %s has an unresolvable base_branch %r; "
                    "falling back to single-commit diff %s — the board will "
                    "show only the last commit",
                    commit_sha, base, diff_range,
                )
        else:
            log.info(
                "commit_sha %s has no recorded base_branch; falling back to "
                "single-commit diff %s — the board will show only the last "
                "commit",
                commit_sha, diff_range,
            )
        proc = subprocess.run(
            ["git", "diff", diff_range, "--no-color"],
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
    # Closed allowlist of intake surfaces — anything else falls back to
    # "board" so an arbitrary client string never reaches Task.source.
    # "mcp" added for the MCP bridge (SCRUM-63): its tasks must stay
    # attributable, and jira sync already filters on source == "jira".
    source = body.source if body.source in ("board", "jira", "mcp") else "board"
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
    # PR-001: honour an explicitly pinned base. Blank/whitespace is treated as
    # "not pinned" so an empty composer field cannot write an empty string that
    # would then beat the fallback (`ctx.get("base_branch") or
    # await self._implicit_base_branch(repo)` — "" is falsy, but an empty key is
    # still misleading to every reader and to the PR-time mismatch warning).
    pinned_base = (body.base_branch or "").strip()
    if pinned_base:
        task.context = {**(task.context or {}), "base_branch": pinned_base}
    if body.backend and body.backend == "claude":
        task.config["backend"] = body.backend
    # GAP 1: opt in to the human plan-approval gate. Never for an imported
    # ticket — see CreateTaskRequest.plan_approval.
    if body.plan_approval and source != "jira":
        from ..core.plan_gate import CONFIG_KEY as _PLAN_APPROVAL_KEY
        task.config[_PLAN_APPROVAL_KEY] = True
    if repo_path:
        from ..profile import apply_default_task_config
        profile = await store.get_profile(repo_path)
        task.config = apply_default_task_config(profile, task.config)
    await store.create_task(task)
    summary = TaskSummaryOut.from_task(
        task, max_pr_conflict_rounds=_max_pr_conflict_rounds())
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({
        "type": "task_created",
        "task_id": task.id,
        "tasks": [t.model_dump() for t in tasks],
    })
    return summary


async def _record_intake_spend(store, site: str, model: str | None, obj) -> None:
    """Book one intake call's tokens to the unattributed ledger, never raising.

    *obj* is any intake result carrying the three token fields (``GrillQuestion``
    / ``GrillResult`` / ``EvalResult``). Accounting is not allowed to break a
    request: a ledger write that fails degrades the record, not the intake.
    """
    if obj is None:
        return
    try:
        await store.record_unattributed_usage(
            site=site,
            model=model,
            tokens_used=getattr(obj, "tokens_used", 0),
            cache_read_tokens=getattr(obj, "cache_read_tokens", 0),
            cache_creation_tokens=getattr(obj, "cache_creation_tokens", 0),
        )
    except Exception as exc:  # noqa: BLE001 — accounting never blocks intake
        log.warning("intake usage not recorded for %s: %s", site, exc)


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
    # This round's utility-tier spend. It cannot go on an attempt row: the
    # wizard runs before any task exists (and the operator may never finish
    # it), so it is booked to the unattributed intake ledger instead of being
    # forced onto some later task that did not ask for it.
    await _record_intake_spend(store, "api.grill", config.review_model, step)
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
            # Same unattributed-ledger booking as the sync endpoint above.
            await _record_intake_spend(
                store, "api.grill_stream", config.review_model, step)
            if isinstance(step, GrillResult):
                # D1/D9: run evaluator and emit verdict before grill_result.
                try:
                    from ..intake.evaluator import evaluate_spec
                    eval_result = await evaluate_spec(
                        step.title, step.description, step.acceptance_criteria,
                        model=config.utility_model,
                    )
                    await _record_intake_spend(
                        store, "api.grill_stream.evaluate_spec",
                        config.utility_model, eval_result)
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
        out.append(TaskSummaryOut.from_task(
            t, _latest_pr_url(attempts), attempts=attempts,
            max_pr_conflict_rounds=_max_pr_conflict_rounds()))
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
    base = (ctx.get("base_branch") or "").strip() or None
    for a in reversed(attempts):
        sha = a.get("commit_sha")
        if sha:
            return _git_diff(task.repo_path, sha, base)
    return ""


def _review_pass_evidence(context: dict, head_sha: str, repo) -> tuple[bool, str]:
    """(passed, evidence-line) for the branch's HEAD sha — mirrors the CLI's
    helper of the same name (`cli/commands.py`). Kept local rather than
    shared: `vcs/approve_merge.py` sits below `core/` (`core.orchestrator`
    already imports `vcs` at module scope) so it cannot import Orchestrator
    itself, and this endpoint already imports Orchestrator anyway."""
    history = (context or {}).get("review_history") or []
    if isinstance(history, str):
        import ast
        try:
            history = ast.literal_eval(history)
        except (ValueError, SyntaxError):
            history = []
    if not isinstance(history, list):
        history = []
    rounds = Orchestrator._rounds_for_head(history, head_sha=head_sha, repo=repo)
    if not rounds:
        return False, "no review round is stamped with a commit reachable from the branch head"
    last = rounds[-1] if isinstance(rounds[-1], dict) else {}
    passed = bool(last.get("passed"))
    verdict = "PASS" if passed else "not passed"
    evidence = f"review {verdict} on {head_sha[:12]} after {len(rounds)} round(s)"
    return passed, evidence


@app.post("/api/tasks/{task_id}/approve")
async def approve_task(task_id: str, request: Request) -> dict[str, Any]:
    """Approve and merge — squash-lands the PR under the operator identity.
    The agent itself still never merges anything on its own (constraint #2);
    this endpoint IS the human merge action `nh approve`/the GUI button
    trigger."""
    store = _store(request)
    task = await _require_task(store, task_id)
    if task.status != TaskStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=f"task is {task.status.value!r}, not awaiting_approval",
        )
    # Idempotency guard (409, not the button's disabled state alone): a
    # second approve for the same task — a raced double-click that beat the
    # frontend's own disable, or a second browser tab — must never reach a
    # second `land_task` (a second squash/push race). Database-backed CAS on
    # `context.merge_in_progress` so this holds across server instances, not
    # just in-process.
    if not await store.claim_merge(task.id):
        raise HTTPException(status_code=409, detail="Merge already in progress")
    # Opt-in telemetry: the click itself, nothing about WHAT was approved.
    try:
        from .. import telemetry as _telemetry
        cfg = getattr(request.app.state, "config", None)
        _telemetry.record("approve_clicked",
                          config=cfg.data if cfg is not None else {})
    except Exception:
        pass
    try:
        task.context = await store.merge_context(task.id, {"approved_at": _now()})
        # An already-satisfied claim has no PR to merge — approval IS the human
        # confirmation its terminal promised, so it completes the task (the agent
        # still never merges anything; there is nothing to merge). Guarded on
        # pr_url: the report key persists in context, and after a send-back a
        # LATER attempt may ship a real PR — that approval must stay a merge
        # instruction, never a false DONE (PR #101 round-2 review).
        message = "Approval recorded. Merge the PR in your git host — the agent never merges."
        landed_sha = ""
        completed_landed = False

        loop = asyncio.get_running_loop()

        def on_step(step: str) -> None:
            # Called from the `land_task` worker thread (asyncio.to_thread) —
            # bridge back onto the event loop so the broadcast can await the
            # websocket sends. `_emit_task_event` never raises.
            asyncio.run_coroutine_threadsafe(
                _emit_task_event(store, task.id, f"merge_step_{step}", f"merge: {step}"),
                loop,
            )

        async def _merge(pr_url: str) -> tuple[str, dict[str, str] | None]:
            await _emit_task_event(store, task.id, "merge_started", "merge started")
            return await _merge_task_pr(request, store, task, pr_url, on_step=on_step)

        if (task.context or {}).get("already_satisfied_report"):
            # Guarded on `task_has_pr_evidence`, not `attempts.pr_url` alone (live
            # incident, task 8c8b36b5): a draft PR opened pre-review is recorded
            # only in `context["pr_draft_created"]` or a `pr_draft` event, never
            # on an attempt row — reading attempts alone missed it and completed
            # the task while its PR sat open.
            pr_url = await task_has_pr_evidence(store, task)
            if not pr_url:
                await store.set_status(
                    task, TaskStatus.DONE, validate=False,
                    event={"source": "human", "kind": "approved_already_satisfied",
                           "text": "already-satisfied claim confirmed by approve"},
                )
                message = ("Already satisfied claim confirmed — no code change was "
                           "needed. Task done (there is no PR; the agent never merges).")
            else:
                landed_sha, error_detail = await _merge(pr_url)
                if error_detail:
                    raise HTTPException(status_code=500, detail=error_detail)
                if landed_sha:
                    message = _merge_outcome_message(landed_sha)
                else:
                    completed_landed, message = await _landed_completion_outcome(
                        store, task, landed_sha)
        else:
            pr_url = await task_has_pr_evidence(store, task)
            if pr_url:
                landed_sha, error_detail = await _merge(pr_url)
                if error_detail:
                    raise HTTPException(status_code=500, detail=error_detail)
                if landed_sha:
                    message = _merge_outcome_message(landed_sha)
                else:
                    completed_landed, message = await _landed_completion_outcome(
                        store, task, landed_sha)
        tasks = await _board_tasks(store, scheduler=_sched(request))
        await _mgr.broadcast({
            "type": "task_approved",
            "task_id": task.id,
            "tasks": [t.model_dump() for t in tasks],
        })
        if landed_sha or completed_landed:
            await _mgr.broadcast({
                "type": "task_updated", "task_id": task.id,
                "status": TaskStatus.DONE.value,
                "tasks": [t.model_dump() for t in tasks],
            })
        return {
            "ok": True,
            "message": message,
            "landed_sha": landed_sha,
        }
    finally:
        await store.release_merge(task.id)


@app.post("/api/tasks/{task_id}/approve-landed")
async def approve_landed(
    task_id: str, body: LandedOverrideRequest, request: Request,
) -> dict[str, Any]:
    """The HUMAN landed-override affirmation: a human asserts (with required
    justification) that a task's content landed at ``sha``, for either of two
    narrow shapes ``blockers/landed_override.py`` resolves and gates:

    - an ``awaiting_approval`` task where automated containment honestly
      refuses (a supervising session's squash train adapted the content: a
      later train car's classification-decision edits, or a real
      union-resolved source conflict, so no candidate commit's tree matches
      the branch verbatim), or
    - a ``failed`` task that died before ever opening a PR (budget
      exhaustion, a pre-review test failure, a compile error) whose content
      a human later hand-landed — refused if the task was human-cancelled or
      already has PR evidence (that pair goes through
      ``nh task restore-approval`` instead).

    See ``blockers/landed_override.py`` for the full contract; this endpoint
    only cheap-guards obviously-ineligible statuses and otherwise delegates
    every eligibility decision to that module.

    This is deliberately additive and non-idempotent: a second call on the
    same task 409s (the task is DONE), so a replay cannot append a duplicate
    override event. It never merges, pushes, or touches git state — the
    override is a recorded human assertion, not a merge action (constraint
    #2: the agent never merges; there is nothing to merge here)."""
    from ..blockers.landed_override import OverrideRefused, approve_landed_override

    store = _store(request)
    task = await _require_task(store, task_id)
    if task.status not in (TaskStatus.AWAITING_APPROVAL, TaskStatus.FAILED):
        raise HTTPException(
            status_code=409,
            detail=(
                f"task is {task.status.value!r}, not awaiting_approval or "
                "a pre-PR failed task"
            ),
        )
    try:
        result = await approve_landed_override(store, task, body.sha, body.justification)
    except OverrideRefused as exc:
        raise HTTPException(status_code=400, detail=exc.reason)

    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({
        "type": "task_approved",
        "task_id": task.id,
        "tasks": [t.model_dump() for t in tasks],
    })
    await _mgr.broadcast({
        "type": "task_updated", "task_id": task.id,
        "status": TaskStatus.DONE.value,
        "tasks": [t.model_dump() for t in tasks],
    })
    return {
        "ok": True,
        "message": result["text"],
        "sha": result["sha"],
        "residue": result["residue"],
    }


async def _landed_completion_outcome(store, task, landed_sha: str) -> tuple[bool, str]:
    """After a no-op `_merge_task_pr` (``landed_sha == ""``), tell whether
    that no-op was the landed-completion path (`complete_if_approved_and_landed`
    already wrote DONE) rather than one of the existing skip/refusal paths
    (task still AWAITING_APPROVAL — no branch/repo recorded, an unresolvable
    branch, or `land_task` deciding it is disabled/has nothing to merge).
    Re-reads the row rather than threading a third return value through
    `_merge_task_pr`, since every existing "" -> no-op path there leaves the
    task AWAITING_APPROVAL and only this new path writes DONE."""
    assert not landed_sha
    refreshed = await store.get_task(task.id)
    if refreshed is not None and refreshed.status == TaskStatus.DONE:
        return True, ("Content is already on the default branch — approval "
                       "recorded and task completed; no merge was attempted.")
    return False, _merge_outcome_message(landed_sha)


def _merge_outcome_message(landed_sha: str) -> str:
    if landed_sha:
        return f"Approved and merged — landed {landed_sha[:12]} onto the default branch."
    return "Approval recorded. Merge the PR in your git host — the agent never merges."


async def _merge_task_pr(
    request: Request, store, task, pr_url: str,
    on_step: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, str] | None]:
    """Land the PR (vcs/approve_merge.land_task) off the event loop.

    Returns ``(landed_sha, error_detail)``. ``landed_sha`` is "" on any
    skip/refusal/failure. ``error_detail`` is ``None`` on a clean skip (no
    repo/branch to merge, `land_task` itself decided `approve_merge.enabled`
    is false, etc — approval stays recorded, today's record-only message
    stands) and is ``{"step": ..., "stderr": ...}`` on a genuine land
    failure or a failed review-PASS precondition — the caller turns that
    into an `HTTPException(500, ...)` so the failure is surfaced to the
    human rather than silently read as success (plan §3/4). The task is
    marked DONE by the caller only when a sha comes back."""
    config = request.app.state.config
    if not task.repo_path:
        return "", None
    from ..vcs.approve_merge import land_task
    from ..vcs.git import GitError, GitRepo
    from ..vcs.task_pr import resolve_task_pr

    resolved = await resolve_task_pr(store, task)
    branch = resolved.branch
    if not branch:
        return "", None

    from ..blockers.shipped import complete_if_approved_and_landed
    landed = await complete_if_approved_and_landed(store, task, pr_url, branch=branch)
    if landed is not None:
        # Content is already on the default branch (a closed-PR squash train,
        # most often) — the task is DONE (or was already terminal) and no
        # merge was ever attempted. `approve_task` re-reads the row to build
        # its message/broadcast rather than this function returning a third
        # value, since the existing "" == "no merge happened" callers all
        # stay correct either way.
        return "", None

    def _resolve_head() -> tuple[str, GitRepo | None]:
        try:
            repo = GitRepo(
                Path(task.repo_path),
                identity_name=config["git"]["agent_identity_name"],
                identity_email=config["git"]["agent_identity_email"],
                never_push_to=config["git"]["never_push_to"],
            )
            repo.fetch()
            ref = repo.resolve_commitish(branch)
            return (repo._run("rev-parse", ref) if ref else ""), repo
        except (GitError, OSError):
            return "", None

    head_sha, repo = await asyncio.to_thread(_resolve_head)
    if not head_sha or repo is None:
        return "", None
    passed, evidence = _review_pass_evidence(task.context or {}, head_sha, repo)
    if not passed:
        return "", {"step": "preconditions", "stderr": evidence}

    result = await asyncio.to_thread(
        land_task,
        repo_path=task.repo_path, branch=branch, pr_url=pr_url,
        task_id=task.id, task_title=task.title, review_evidence=evidence,
        config=config.data, on_step=on_step,
    )
    if result.skipped:
        return "", None
    if not result.ok:
        await _emit_task_event(
            store, task.id, "merge_failed",
            f"merge failed at {result.step}: {result.stderr[:200]}",
        )
        return "", {"step": result.step, "stderr": result.stderr}
    await store.set_status(
        task, TaskStatus.DONE, validate=False,
        event={"source": "human", "kind": "human_merged",
               "sha": result.landed_sha, "text": result.message},
    )
    await _emit_task_event(
        store, task.id, "human_merged", result.message, persist=False,
    )
    return result.landed_sha, None


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
    await store.set_status(
        task, TaskStatus.DONE, validate=False,
        event={"source": "human", "kind": "review_finished",
               "text": f"review finished — {posted}/{len(drafts)} comment(s) posted"},
    )
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
    if task.status == TaskStatus.DONE:
        raise HTTPException(status_code=409, detail="task is already done")
    if task.status == TaskStatus.FAILED and (task.context or {}).get("cancel_reason"):
        raise HTTPException(status_code=409, detail="task is cancelled")
    await store.append_context_list(
        task.id, "send_back_feedback", {"at": _now(), "message": body.message})
    # A human pressing "Send back" IS the gate the zero-diff honesty check looks
    # for, so record that this re-entry is theirs. No checkpoint is involved
    # here, so the write CLEARS any recorded `sha`/`branch` rather than
    # relabelling a sha this human never chose — relabelling is what disarmed
    # the gate and credited the loop's own abandoned partial.
    from ..blockers import resume_provenance
    # A CLEAR is a clear however it is spelled: this writes `sha: None`, and
    # the orphan sweep reads a `resume_from` with no sha exactly as it reads no
    # `resume_from` at all — so without this the sweep would re-stamp the dead
    # attempt's sha over the human's decision, with MACHINE provenance.
    await store.close_open_attempts(task.id)
    task.context = await store.merge_context(
        task.id, {"resume_from": resume_provenance(None, "human")})
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


# ESCALATED is the state a task is in when it is asking a human to decide —
# exactly the one a human most needs to be able to hold, same as a
# supervisor reserving the quota window (SCRUM-58).
_HOLDABLE_STATUSES = {TaskStatus.PAUSED_QUOTA, TaskStatus.BLOCKED, TaskStatus.ESCALATED}


@app.post("/api/tasks/{task_id}/pause")
async def pause_task(
    task_id: str, request: Request,
) -> dict[str, Any]:
    """Pause a running task (sets to BLOCKED with reason). For a task already
    parked (paused_quota/blocked) — e.g. a supervisor reserving the quota
    window — this instead stamps a durable human hold (blocker.human_stopped)
    on the existing blocker without touching status; the wake sweep already
    skips human_stopped tasks (SCRUM-22)."""
    store = _store(request)
    task = await _require_task(store, task_id)
    if task.status in _HOLDABLE_STATUSES:
        blocker_data = dict(task.blocker or {})
        blocker_data.setdefault("category", "USER_PAUSED")
        blocker_data.setdefault("question", "Paused from board")
        blocker_data["human_stopped"] = True
        task.blocker = blocker_data
        await store.update_task_columns(task)
        tasks = await _board_tasks(store, scheduler=_sched(request))
        await _mgr.broadcast({"type": "task_updated", "task_id": task.id,
                              "tasks": [t.model_dump() for t in tasks]})
        return {"ok": True, "message": f"Held {task_id[:8]}"}
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
    """Resume a paused/blocked/escalated task (sets to IMPLEMENTING). If the
    task carries a durable human hold (blocker.human_stopped, set by /pause
    on an already-parked task), this only clears that flag — the task stays
    in its current parked status so the wake sweep can decide the next
    transition, rather than resume forcing one."""
    store = _store(request)
    task = await _require_task(store, task_id)
    if task.status not in _PARKED_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"task is {task.status.value!r} — only parked tasks can be resumed",
        )
    if isinstance(task.blocker, dict) and task.blocker.get("human_stopped"):
        blocker_data = dict(task.blocker)
        del blocker_data["human_stopped"]
        task.blocker = blocker_data
        await store.update_task_columns(task)
        tasks = await _board_tasks(store, scheduler=_sched(request))
        await _mgr.broadcast({"type": "task_updated", "task_id": task.id,
                              "tasks": [t.model_dump() for t in tasks]})
        return {"ok": True, "message": f"Released hold on {task_id[:8]}"}
    # Read the checkpoint BEFORE clearing the blocker, which is what holds the
    # sha — exactly as `nh task resume` does. This endpoint is the Resume button
    # in the drawer, and it used to do neither: it dropped the blocker on the
    # floor, so the next attempt branched from a stale `resume_from` (or from
    # base) and silently threw away everything the parked attempt had already
    # committed, and it left the previous actor's `by` describing a resume a
    # human had just performed. Two independent reviews found this same hole.
    from ..blockers import resume_checkpoint, resume_provenance
    checkpoint = resume_checkpoint(task.blocker)
    task.blocker = None
    task.wake_check_at = None
    await store.update_task_columns(task)
    task.context = await store.merge_context(
        task.id, {"resume_from": resume_provenance(checkpoint, "human")})
    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({"type": "task_updated", "task_id": task.id,
                          "tasks": [t.model_dump() for t in tasks]})
    return {"ok": True, "message": f"Resumed {task_id[:8]} → implementing"}


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str, request: Request, body: CancelRequest | None = None,
) -> dict[str, Any]:
    """Cancel a task (sets to FAILED). `body` is optional so the CLI's and
    the board's pre-existing no-reason POST keep working unchanged; when the
    board's cancel modal supplies a typed reason it is trimmed and truncated
    to 500 chars before being recorded, matching the client-side clamp."""
    store = _store(request)
    task = await _require_task(store, task_id)
    if task.status in {TaskStatus.DONE, TaskStatus.FAILED}:
        raise HTTPException(
            status_code=409,
            detail=f"task is already {task.status.value!r}",
        )
    reason = (body.reason if body else None) or ""
    reason = reason.strip()[:500] or "Cancelled from board"
    task.context = await store.merge_context(
        task.id, {"cancel_reason": reason})
    await store.set_status(
        task, TaskStatus.FAILED, validate=False, human_override=True)
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


def _windows_kill_by_cmdline(task_id: str) -> int:
    """Windows equivalent of ``pkill -9 -f <task_id>``. Returns 1 if it ran.

    Windows has NO built-in kill-by-command-line, so this is two steps rather
    than one, and the choice between the candidates matters:

    * ``wmic`` would do it in one call, but it is deprecated and REMOVED from
      Windows 11 24H2 onward — a cleanup that silently stops working on new
      machines is the same class of defect as the ``pkill`` that was never
      there.
    * ``taskkill`` can only match an image name or a PID, never a command line,
      so on its own it cannot find a task's children at all.

    So: enumerate PIDs with PowerShell over ``Win32_Process.CommandLine``
    (present on every supported Windows), then ``taskkill /F /T`` each one.
    ``/T`` also takes the process TREE, which is what "the task's SDK and
    pytest subprocesses" actually means and which ``pkill -f`` only achieved
    because each child carried the id in its own argv.

    UNTESTED ON WINDOWS — no Windows host was available. What is tested here is
    the argv shape, the self-exclusion, and that the branch is taken at all.
    """
    # The id is interpolated into a PowerShell string, so it must not be able
    # to carry quoting. Task ids are 32-hex; anything else is refused rather
    # than escaped, because an escaping bug here is a command injection.
    if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
        log.warning("cancel: refusing to match on a non-alphanumeric task id")
        return 0
    # Both this PowerShell and our own process carry the id in their command
    # lines, so both are excluded — otherwise the cleanup kills the server.
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-CimInstance Win32_Process | Where-Object { "
        + f"$_.CommandLine -like '*{task_id}*' -and $_.ProcessId -ne $PID "
        + f"-and $_.ProcessId -ne {os.getpid()} "
        + "} | ForEach-Object { $_.ProcessId }"
    )
    enum = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=20,
    )
    if enum.returncode != 0:
        return 0
    for line in (enum.stdout or "").split():
        if not line.isdigit():
            continue
        subprocess.run(["taskkill", "/F", "/T", "/PID", line],
                       capture_output=True, timeout=10)
    return 1


async def _kill_task_processes(task_id: str) -> int:
    """Best-effort kill of a task's worktree subprocesses (SDK + pytest) by its
    unique id. Returns how many pkill patterns matched (for tests/telemetry)."""
    if not task_id or len(task_id) < 12:  # never pkill on a too-broad pattern
        return 0
    try:
        if _IS_WINDOWS:
            return await asyncio.to_thread(_windows_kill_by_cmdline, task_id)
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
    #
    # `resume_from` is cleared for the same reason, and it is load-bearing: this
    # endpoint promises "a fresh run", and a fresh run must not silently branch
    # from a checkpoint some EARLIER actor chose. Leaving it behind meant the
    # zero-diff honesty gate judged the retry by a decision nobody made for it —
    # and if that stale pair carried `by: "human"`, the gate was disarmed for a
    # run no human had gated. Retry means from base; a human who wants to
    # continue from a checkpoint has Resume for that.
    # And the attempt rows the dead worker left `in_progress` are retired with
    # it. The orphan sweep re-derives a checkpoint from exactly those rows, so
    # clearing the context alone let the next sweep undo this endpoint's whole
    # promise — see `Store.close_open_attempts`.
    await store.close_open_attempts(task.id)
    task.context = await store.merge_context(
        task.id, {"cancel_reason": None, "retried_at": _now(),
                  "resume_from": None})
    await store.update_task_columns(task)
    await store.set_status(
        task, TaskStatus.PENDING, validate=False, human_override=True)
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({"type": "task_updated", "task_id": task.id,
                          "tasks": [t.model_dump() for t in tasks]})
    return {"ok": True, "message": f"Retried {task_id[:8]} → pending"}


@app.post("/api/tasks/{task_id}/shipped", response_model=TaskOut)
async def mark_shipped(
    task_id: str, body: ShippedRequest, request: Request,
) -> TaskOut:
    """Record that a human (the supervising session) merged this task's work
    outside no_human — e.g. a squash-merge done by hand after review.

    Non-goal (SCRUM-55 post-mortem): ``sha`` is recorded as operator
    testimony ONLY. It is never fetched, resolved against a ref, or verified
    against any git remote — SCRUM-55 built exactly that verification and
    burned its whole budget on a stale-trunk check plus an unrunnable commit
    test. This endpoint deliberately does not re-add it: the supervising
    human is the trust anchor here, not git.
    """
    store = _store(request)
    task = await _require_task(store, task_id)

    if task.status == TaskStatus.DONE:
        raise HTTPException(status_code=409, detail="task is already done")
    if task.status == TaskStatus.FAILED and (task.context or {}).get("cancel_reason"):
        raise HTTPException(status_code=409, detail="task is cancelled")
    # _SHIPPABLE allow-list removed — operator-testimony model: the
    # supervising human is the trust anchor, so shipped is valid from any
    # non-terminal status (SCRUM-69). done and cancelled remain 409 above.

    sha = body.sha.strip()
    if not sha:
        raise HTTPException(status_code=400, detail="sha must not be empty")

    task.blocker = None
    task.wake_check_at = None
    await store.update_task_columns(task)
    await store.set_status(
        task, TaskStatus.DONE, validate=False, human_override=True,
        event={"source": "human", "kind": "human_merged",
               "sha": sha, "note": body.note, "ts": time.time()},
    )
    attempts = await store.list_attempts(task.id)
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({
        "type": "task_updated", "task_id": task.id,
        "status": TaskStatus.DONE.value,
        "tasks": [t.model_dump() for t in tasks],
    })
    return TaskOut.from_task(task, attempts)


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
        # `proven`/`is_usable` ride along so a caller can tell "onboarded" from
        # "has a test command the review gate can actually run" — the two the
        # board's chip used to conflate.
        from ..profile import ProjectProfile
        out_rows: list[dict[str, Any]] = []
        for r in rows:
            repo_path = r.get("repo_path", "") or ""
            row = {"repo_path": repo_path,
                   "ecosystem": r.get("ecosystem", ""),
                   "confirmed": bool(r.get("confirmed", False)),
                   "name": repo_path.rstrip("/").rsplit("/", 1)[-1] if repo_path else "",
                   "proven": {}, "test_proven": False, "is_usable": False,
                   "test_cmd": ""}
            if r.get("data"):
                try:
                    row.update(_profile_readiness(
                        ProjectProfile.from_dict(json.loads(r["data"]))))
                except (ValueError, TypeError) as exc:
                    log.warning("unreadable profile row for %s: %s", repo_path, exc)
            out_rows.append(row)
        return out_rows
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
        rows = await store.query(
            """SELECT te.task_id,
                      json_extract(te.data, '$.kind'),
                      snippet(events_fts, 0, '', '', '…', 12),
                      te.ts
               FROM events_fts f
               JOIN task_events te ON te.id = f.rowid
               WHERE events_fts MATCH ? ORDER BY rank LIMIT ?""",
            (query, lim),
        )
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


# What the regex does: allows 1-80 characters drawn only from ASCII letters,
# digits, dot, underscore and hyphen - no separators, no whitespace, nothing
# a shell or path expands. Two shapes that still match the charset are refused
# by explicit checks below: dots-only names ("." ".." "....") and ".git".
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")

# The ONLY variables a git child of this endpoint inherits. An allowlist, not
# a denylist: git reads dozens of environment redirects, and a
# subtract-the-known-bad set lets through every name nobody enumerated.
# GIT_OBJECT_DIRECTORY (objects land outside $HOME while the call still
# answers 201) and GIT_COMMON_DIR (refs/config relocated outside $HOME) both
# slipped a scrub list that already named GIT_DIR and GIT_WORK_TREE.
# Everything the -c flags set (identity, gpgsign) is likewise unreachable from
# the environment here, since GIT_AUTHOR_*/GIT_COMMITTER_* outrank `-c user.*`.
#
# Measured on macOS with `env -i`: `git init`, `git add` and `git commit`
# all exit 0 with nothing but PATH and HOME set. The rest of this set is for
# behavior the operator would notice, not for git to run at all.
_GIT_ENV_KEEP = frozenset({
    "PATH",                        # finding the git binary
    "HOME",                        # git's own home lookups; ~/.gitconfig is
                                   # read but every value we care about is
                                   # pinned by a `-c` flag below
    "TMPDIR",                      # where git writes its temp files
    "LANG", "LC_ALL", "LC_CTYPE",  # message and path encoding
    "TZ",                          # timezone offset stamped on the commit
    # Windows equivalents. Git for Windows resolves `~` from USERPROFILE (or
    # HOMEDRIVE+HOMEPATH) and NOT from HOME, so with only "HOME" on this list
    # the sanitised env had no home at all there and ~/.gitconfig — identity,
    # credential helper, core.autocrlf — was silently never read. SystemRoot
    # and COMSPEC are needed for a process to start at all on Windows; PATHEXT
    # is how the loader finds `git.exe` from the bare name.
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "TEMP", "TMP", "SystemRoot", "SYSTEMROOT", "COMSPEC", "PATHEXT",
})


class ScaffoldRepoRequest(BaseModel):
    parent: str
    name: str


@app.post("/api/repos/scaffold", status_code=201)
async def scaffold_repo(body: ScaffoldRepoRequest, request: Request) -> dict[str, Any]:
    """Create a brand-new git repo and register it as a project.

    The composer's "create a new repo" affordance: mkdir + `git init` + a
    minimal README committed under the AGENT identity (the history must say
    plainly which commits a machine wrote), then the same registration path
    POST /api/projects uses, so the composer proceeds in it immediately.
    """
    # This endpoint writes to the operator's filesystem, so it takes the same
    # posture as the credential routes (a step beyond the read-mostly project
    # siblings): a cross-origin or origin-less browser write is refused, or a
    # drive-by page could litter $HOME with directories while `nh serve` is up.
    _require_local_origin(request, writing=True)
    store = _store(request)
    config = request.app.state.config

    # NOT stripped: a name with any whitespace (including a trailing \n) is
    # rejected by the regex rather than silently laundered into a valid one.
    name = body.name or ""
    if not _REPO_NAME_RE.fullmatch(name):
        raise HTTPException(
            400, "invalid repo name - use letters, digits, '.', '_' or '-' "
                 "(1-80 characters)")
    # "." ".." "...." match the charset regex but are path navigation, not names.
    if set(name) == {"."}:
        raise HTTPException(
            400, "invalid repo name - a dots-only name is path navigation, "
                 "not a name")
    # Case-insensitive: the operator's filesystem usually is too, and a ".git"
    # directory is git's own metadata, not a repo.
    if name.lower() == ".git":
        raise HTTPException(
            400, "invalid repo name - '.git' is git's metadata directory")
    raw_parent = (body.parent or "").strip()
    parent = Path(raw_parent).expanduser()
    if not parent.is_absolute():
        raise HTTPException(400, "parent must be an absolute path")
    # resolve() BEFORE the containment check: a lexically-under-home path can
    # ..-escape it, and a symlinked segment can point anywhere.
    parent = parent.resolve()
    home = Path.home().resolve()
    if parent != home and home not in parent.parents:
        raise HTTPException(400, "parent must be a directory under your home")
    if not parent.is_dir():
        raise HTTPException(400, f"parent is not an existing directory: {parent}")
    target = parent / name
    if target.exists():
        raise HTTPException(409, f"{target} already exists")
    # The project is registered under the repo's name, and project names are
    # unique - so ~/a/dup and ~/b/dup cannot both be registered here. Refuse
    # the second one BEFORE anything is written: a 409 raised after the mkdir
    # leaves a real repo on disk that this endpoint can never register, since
    # the retry stops at the target.exists() check above.
    if await store.get_project_by_name(name):
        raise HTTPException(
            409, f"project {name!r} already exists - pick a different name "
                 f"(nothing was created)")

    git_cfg = config.data.get("git") or {}
    ident_name = git_cfg.get("agent_identity_name", "no_human")
    ident_email = git_cfg.get("agent_identity_email", "no-human@acme.com")

    # True only once THIS request's mkdir succeeded: the error-path cleanup is
    # gated on it, so a dir another writer made in the exists()->mkdir window
    # (mkdir raises FileExistsError) is never deleted as "our" debris.
    created = False

    def _scaffold() -> None:
        nonlocal created
        # Build the child env from _GIT_ENV_KEEP rather than copying
        # os.environ: the server's environment may carry any of git's write
        # redirects or identity overrides, and only names on that list reach
        # the child.
        git_env = {k: v for k, v in os.environ.items() if k in _GIT_ENV_KEEP}
        # With env= passed, exec resolves the binary against THIS PATH; if the
        # server started without one, fall back to the platform default rather
        # than searching an empty path.
        git_env.setdefault("PATH", os.defpath)
        git_env["GIT_CONFIG_NOSYSTEM"] = "1"

        def _git(*args: str) -> None:
            subprocess.run(  # no shell - argv only, nothing interpolated
                ["git", "-C", str(target),
                 "-c", f"user.name={ident_name}",
                 "-c", f"user.email={ident_email}",
                 # The operator's global config must not leak into a machine
                 # commit (gpg signing would block; templates would pollute).
                 "-c", "commit.gpgsign=false",
                 *args],
                check=True, capture_output=True, text=True, timeout=30,
                env=git_env)
        target.mkdir()
        created = True
        _git("init", "-q")
        (target / "README.md").write_text(f"# {name}\n", encoding="utf-8")
        _git("add", "README.md")
        # --no-verify: hooks installed by an init template must not run here.
        _git("commit", "-q", "--no-verify", "-m", f"scaffold {name}")

    try:
        await asyncio.to_thread(_scaffold)
    except Exception as exc:
        # Never leak a stack trace; do log it, and remove the half-made dir so
        # a retry is not an instant 409 on our own debris - but ONLY if this
        # request made the dir (see `created`; never delete another writer's).
        log.warning("repo scaffold failed for %s: %s", target, exc)
        if created:
            import shutil
            shutil.rmtree(target, ignore_errors=True)
        raise HTTPException(500, "creating the repository failed - see server logs")

    from ..project_model import Project
    proj = Project.new(name=name, repo_paths=[str(target)],
                       primary_repo=str(target))
    try:
        await store.create_project(proj)
    except Exception as exc:
        if "UNIQUE" in str(exc):
            # The pre-check above cannot close the window: another writer can
            # take the name between it and this INSERT. Same answer, and the
            # directory this request made goes with it - otherwise the loser
            # of the race is left with the orphan the pre-check exists to
            # prevent.
            if created:
                import shutil
                shutil.rmtree(target, ignore_errors=True)
            raise HTTPException(
                409, f"project {name!r} already exists - pick a different name "
                     f"(nothing was created)")
        raise
    return {"repo_path": str(target), "project_id": proj.id}


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
    # A hand-deleted latest.json must not hide an intact published baseline —
    # 404 means "nothing was ever recorded", and a clean publish WAS recorded.
    if not path.exists() and not published_file().exists():
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
    card = None
    if path.exists():
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
            if baseline_card is None:
                # load() swallows OSError/JSONDecodeError — log it here or a
                # truncated baseline is undiagnosable (same rule as latest.json).
                log.error("published baseline at %s could not be read "
                          "(unparseable or unreadable)", pub_path)
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
             f"trusted"
             if path.exists() else
             f"{path.name} is missing and the published baseline could not "
             f"be read, so no figure below can be trusted"])
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

# The review verdict's substance. `text` says it in prose for a human, but the
# board decides PASS vs FAIL from `passed` and counts the findings from these —
# strip them and a PASSING round renders as "FAIL (? blocking)", which is worse
# than the silence this whole fix replaced. Same reason `message` is carried
# for the supervisor: on these events the meta IS the content.
_VERDICT_META = ("passed", "failed_count", "blocking_count", "advisory_count")


def _format_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    pending_tool_use: dict[str, Any] | None = None  # last tool_use awaiting its result
    for e in events:
        source = e.get("source", "")
        kind = e.get("kind", "")

        # Always include narration — see `is_narration`. This used to be a
        # hand-kept list of narration SOURCES and it drifted twice by omission:
        # `watcher` (the post-PR ladder starved out of the UI — 2015 events
        # served, 0 from watcher, found by the 2026-07-11 persona walk) and then
        # `reviewer` (the review verdict itself invisible). Both copies of the
        # list now ask the one predicate instead.
        if is_narration(source, kind) or kind in ("result", "error"):
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
            for key in _VERDICT_META:
                if key in e:
                    entry[key] = e[key]
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
                # Narration passes through live exactly as it does in
                # _format_events — the same predicate, so the replayed log and
                # the live stream can never disagree about what a human sees.
                if is_narration(source, kind) or kind in ("result", "error"):
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
                for key in _VERDICT_META:
                    if key in e:
                        frame_data[key] = e[key]
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


_STALE_TTL_SECONDS = 60.0
# None means "never computed", which is NOT the same as "computed, found
# current" (a cached None). A (0.0, None) seed conflates them, and on a
# platform where time.monotonic() starts near zero it serves an answer nobody
# ever calculated for the first minute of the process's life.
_stale_cache: tuple[float, str | None] | None = None
_stale_inflight = threading.Lock()


def _loaded_code_stale() -> str | None:
    """The advisory staleness note, recomputed at most once a minute.

    HEAD moves after startup, so this cannot be a startup-time constant — but
    every open board tab polls this endpoint on a timer and each check is a git
    subprocess, so it is cached. Purely informational: no caller gates on it,
    and by design nothing here can stop a task being claimed.

    Single-flight, and NON-BLOCKING about it. A cache miss alone is not enough
    to serialize on: the miss window is however long `staleness_note` takes,
    and the case this feature exists to detect is exactly the one where git is
    slow. Measured, rather than assumed: the board polls every 10s
    (`App.jsx`: `setInterval(poll, 10000)`), and with `loaded_code()` already
    warmed this path makes 1 git call when current and 2 when stale
    (`rev-parse`, then `merge-base`) — a 10-20s ceiling under `_GIT_TIMEOUT`,
    NOT `_detect`'s 30s, which is a different path. So a slow measurement
    outlives roughly two polls per open tab, and every one of those would start
    its own git: a process herd in precisely the degraded condition being
    measured, growing with the number of tabs.

    A loser therefore returns the LAST KNOWN answer instead of waiting. Waiting
    would trade a git herd for a thread-pool herd: these run under
    `asyncio.to_thread`, whose executor holds `min(32, cpu_count + 4)` threads
    — 16 here — so a dozen-odd waiters parked for the full ceiling would leave
    the whole process about one worker. Serving a slightly stale advisory value
    costs nothing: the note is already up to 60s old by design.

    With no cached value at all, a loser returns None, which renders as "no
    banner" — indistinguishable from "current". That is deliberate: during the
    first cold miss under concurrency the honest options are silence or a claim
    we have not finished checking, and silence errs toward not fabricating a
    staleness warning. It self-heals on the next poll.
    """
    global _stale_cache
    cached = _stale_cache          # read once; another thread may swap it
    now = time.monotonic()
    if cached is not None and now - cached[0] < _STALE_TTL_SECONDS:
        return cached[1]
    if not _stale_inflight.acquire(blocking=False):
        return cached[1] if cached is not None else None
    try:
        from ..core.build_info import staleness_note
        note = staleness_note()
        # Stamp completion time, not entry time: with a slow git the two differ
        # by most of the TTL, and stamping entry would re-arm the miss almost
        # immediately and reopen the herd this guard just closed.
        _stale_cache = (time.monotonic(), note)
        return note
    finally:
        _stale_inflight.release()


@app.get("/api/worker/status")
async def worker_status(request: Request) -> dict[str, Any]:
    """Is the embedded worker running, how many tasks in-flight — and if none,
    WHY none?

    The first two fields alone cannot tell idle from wedged. On 2026-08-01 this
    endpoint returned `{"running":true,"inflight":0,"max_workers":4,
    "watcher_error":null}` for six hours while the scheduler's database view was
    frozen three hours in the past, re-dispatching two finished tasks ~12x/min
    and unable to see the one real task waiting. Every field was accurate.

    So `inflight` keeps its meaning and `idle_reason` supplies the one bit it
    never carried: `queue_empty` (nothing to do) vs `db_view_stale` (the queue
    only LOOKS empty) vs `quota_cooldown` vs `claimable_not_dispatched`. The
    counters beside it — crash rate, consecutive status-write failures, last
    successful write, stale detections and reconnects — are what makes the
    difference checkable from a single `curl` rather than by reading 46,000
    lines of log.

    ONE HONEST LIMIT, because it was measured rather than assumed. The
    staleness probe runs per scheduler TICK, not per request — a status
    endpoint that opened a second SQLite connection on every poll would put the
    board's polling on the database's critical path. So a poll landing between
    the wedge and the next tick still answers `healthy: true`; the flag is at
    most one `poll_interval` behind. `seconds_since_last_tick` is published for
    exactly that reason, and it doubles as the alarm for the failure no
    per-tick check can report on itself: a scheduler loop that has stopped
    ticking at all.

    `loaded_code` / `loaded_code_stale` answer a different question on the same
    poll: WHICH code is running. The server never reloads, so a merged fix is
    not live until it restarts.
    """
    sched = getattr(request.app.state, "scheduler", None)
    watcher_error = getattr(request.app.state, "watcher_error", None)
    # Set by the worker task's done-callback. `running: true` means "a Scheduler
    # object is wired up", which is NOT the same as "the loop is alive" — the
    # loop can die and leave the object answering.
    worker_error = getattr(request.app.state, "worker_error", None)
    common = {
        "watcher_error": watcher_error,
        "worker_error": worker_error,
        "loaded_code": getattr(request.app.state, "loaded_code", None),
        "loaded_code_stale": await asyncio.to_thread(_loaded_code_stale),
    }
    if sched is None:
        return {"running": False, "inflight": 0, "max_workers": 0, **common,
                "idle_reason": "no_scheduler",
                "db_view_stale": False, "healthy": False}
    out: dict[str, Any] = {
        "running": True,
        "inflight": len(sched.inflight),
        "max_workers": sched.max_workers,
        **common,
    }
    snapshot = getattr(sched, "health_snapshot", None)
    if callable(snapshot):
        try:
            out.update(snapshot())
        except Exception as exc:  # noqa: BLE001 — status must always answer
            out["health_error"] = f"{type(exc).__name__}: {exc}"
    else:
        # FAIL CLOSED. Defaulting `healthy` to true for an object that cannot
        # describe itself is the same fail-open this change removed everywhere
        # else. Unreachable in production (one assignment site, always a real
        # Scheduler), which is exactly why it must not be left to luck.
        out["health_error"] = ("the scheduler cannot report its health "
                               "(no health_snapshot)")
    # One boolean for the surfaces that only want a light. Every clause is a
    # state that used to render as green, and the last two were added after a
    # review found the first version still had two reachable modes resolving to
    # `healthy: true` / `queue_empty` — the exact pre-incident reading:
    #
    #   * `tick_stalled` — the scheduler loop has stopped ticking. Every other
    #     field here is WRITTEN by a tick, so a stalled loop freezes them all in
    #     their last-known-good state and this endpoint reports the past.
    #     Publishing `seconds_since_last_tick` without consuming it surfaced
    #     nothing; a field with no reader is not a signal.
    #   * `consecutive_probe_failures` — the staleness detector itself is
    #     failing. That means the view is UNKNOWN, and "unknown" must not
    #     resolve to "healthy", or the detector fails open into the very silence
    #     it was built to break.
    #   * `worker_error` — the loop is DEAD. Nothing else here can say so:
    #     every other field is written by a tick, and a loop that died before
    #     its first tick leaves them all at their initial values.
    #
    # `tick_stalled` now also covers a loop that has NEVER ticked and has had
    # longer than its own threshold to do so, which is the same fault one step
    # earlier and reported `healthy: true` permanently until a review found it.
    # The two are complementary and neither replaces the other: the callback
    # catches a loop that DIED, the threshold catches one that is alive but
    # wedged inside a call that never returns — where no callback ever fires.
    out["healthy"] = (
        not out.get("db_view_stale", False)
        and not out.get("tick_stalled", False)
        and not out.get("consecutive_probe_failures", 0)
        and not out.get("consecutive_status_write_failures", 0)
        and watcher_error is None
        and worker_error is None
        and "health_error" not in out
    )
    return out


@app.get("/api/queue/health")
async def queue_health_endpoint(request: Request) -> dict[str, Any]:
    """D2 #4: is the queue stuck, and when does it drain? Pure timestamps."""
    from ..core.health import queue_health
    store: Store = request.app.state.store
    sched = getattr(request.app.state, "scheduler", None)
    inflight = set(sched.inflight) if sched is not None else set()
    max_workers = sched.max_workers if sched is not None else 0
    h = await queue_health(store, inflight_ids=inflight, max_workers=max_workers)
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
        answer_record,
        apply_action,
        is_plan_approval_action,
        is_terminal_action,
        resume_checkpoint,
        resume_provenance,
    )
    from ..core import plan_gate
    from ..core.bounds import Bounds

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
    answer, applied, terminal, approves_plan = body.answer, None, False, False
    if body.choose is not None:
        options = Blocker.from_dict(blocker).options if blocker else []
        if not 1 <= body.choose <= len(options):
            raise HTTPException(
                status_code=400, detail=f"choose must be between 1 and {len(options)}",
            )
        option = options[body.choose - 1]
        answer = option.label
        terminal = is_terminal_action(option.action)
        approves_plan = is_plan_approval_action(option.action)
        try:
            applied = apply_action(
                task, option.action,
                # The install's effective bounds, so a stamped cap is the one
                # the gate will enforce (see `actions._normalised`).
                bounds=Bounds.from_config(
                    (getattr(request.app.state, "config", None)
                     or {}).get("bounds")))
        except ActionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    record = answer_record(
        question=question, answer=answer,
        attempt_id=(task.blocker or {}).get("attempt_id") or "",
        source="operator:api",
    )
    record["applied"] = applied
    await store.append_context_list(task.id, "human_replies", record)
    # Terminal option (SCRUM-22): the human chose "stop — keep parked". Record
    # the answer and leave the parked status untouched; resuming here is what
    # silently inverted the stop.
    if terminal:
        # Review 2026-07-25: without this stamp the wake watcher's sweep
        # undoes the stop — max_park re-escalates within 48h and any
        # wake_condition on the blocker RESUMES the task. The stamp makes the
        # human's decision durable; _evaluate skips human-stopped tasks.
        blocker_data = dict(task.blocker or {})
        blocker_data["human_stopped"] = True
        task.blocker = blocker_data
        task.wake_check_at = None
        await store.update_task_columns(task)
        tasks = await _board_tasks(store, scheduler=_sched(request))
        await _mgr.broadcast({
            "type": "task_updated", "task_id": task.id,
            "status": task.status.value,
            "tasks": [t.model_dump() for t in tasks],
        })
        return {"ok": True, "status": task.status.value, "kept_parked": True}
    patch: dict[str, Any] = {"wake_check_at": None}
    # Continue from the [WIP-BLOCKED] checkpoint rather than from base.
    checkpoint = resume_checkpoint(blocker)
    # Stamp the HUMAN provenance UNCONDITIONALLY — see `WakeWatcher._resume`.
    # The zero-diff honesty gate credits work ahead of base only when a human
    # gated it, and this stamp must OVERRIDE any `by: "wake"` an earlier machine
    # resume left behind. Writing it only `if checkpoint` is what let the stale
    # value survive: RFC 7396 merges nested dicts, so a blocker carrying no
    # `resume_commit` left the previous actor's `by` describing this answer, and
    # the human's reply was failed as fabrication.
    patch["resume_from"] = resume_provenance(checkpoint, "human")
    # GAP 1 plan-approval gate: at the gate, only the approve OPTION approves —
    # free text is a correction, which resumes into PLANNING to be re-planned
    # rather than into IMPLEMENTING. "At the gate" is read off the blocker the
    # human is actually answering (`plan_gate.at_gate`), not off a context
    # flag: nothing cleared that flag, so a stale one hijacked a later,
    # unrelated answer back into planning. Off the gate this is inert and the
    # resume target is IMPLEMENTING exactly as before.
    resume_to = plan_gate.resume_status(task, approve=approves_plan)
    if plan_gate.at_gate(task):
        patch[plan_gate.CONTEXT_KEY] = plan_gate.reply_patch(
            task, approve=approves_plan, answer=answer or "")
    task.context = await store.merge_context(task.id, patch)
    task.wake_check_at = None
    await store.update_task_columns(task)
    await store.set_status(task, resume_to, validate=False)
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({
        "type": "task_updated",
        "task_id": task.id,
        "status": resume_to.value,
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


async def _with_usage_counts(
    store: Store, items: list[Any],
) -> list[dict[str, Any]]:
    """Attach the memory-lifecycle-A outcome split (success/failure/
    cancelled/timeout counts) to each memory row, for the Rules/Skills/
    Learnings panels. `use_count`/`last_used_at` are already columns on
    `memories` and travel with `dict(r)`; only the split needs a join.
    Correlational, not causal — the web cards must label it so."""
    rows = [dict(r) for r in items]
    counts = await store.memory_outcome_counts([r["id"] for r in rows if r.get("id")])
    zero = {"success_count": 0, "failure_count": 0,
            "cancelled_count": 0, "timeout_count": 0}
    for r in rows:
        r.update(counts.get(r.get("id"), zero))
    return rows


@app.get("/api/rules")
async def list_rules(
    request: Request, include_archived: bool = False,
) -> list[dict[str, Any]]:
    store = _store(request)
    from ..learning import TYPE_RULE, TYPE_ANTI_PATTERN
    items = await store.list_memories(
        confirmed=True, mem_type=TYPE_RULE, include_archived=include_archived)
    items += await store.list_memories(
        confirmed=True, mem_type=TYPE_ANTI_PATTERN, include_archived=include_archived)
    return await _with_usage_counts(store, items)


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
async def list_skills(
    request: Request, include_archived: bool = False,
) -> list[dict[str, Any]]:
    store = _store(request)
    from ..learning import TYPE_SKILL, TYPE_FACT
    items = await store.list_memories(
        confirmed=True, mem_type=TYPE_SKILL, include_archived=include_archived)
    items += await store.list_memories(
        confirmed=True, mem_type=TYPE_FACT, include_archived=include_archived)
    return await _with_usage_counts(store, items)


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
    return await _with_usage_counts(store, rows)


# Registered BEFORE any `/api/learnings/{mem_id}` route (constraint noted in
# PLAN.md — belt-and-suspenders even though the method/suffix already
# disambiguate it from the POST .../{mem_id}/retire below) so a literal path
# segment is never captured by a path parameter.
@app.get("/api/learnings/retire-candidates")
async def learnings_retire_candidates(
    request: Request, days: int = 90,
) -> list[dict[str, Any]]:
    """Memory lifecycle C, AC2: stale ACTIVE (confirmed) rules — SUGGEST
    only. Read-only; nothing here archives anything."""
    store = _store(request)
    from ..learning.retire import retirement_candidates
    rows = await retirement_candidates(store, days=days)
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


@app.post("/api/learnings/{mem_id}/retire")
async def retire_learning(mem_id: str, request: Request) -> dict[str, Any]:
    """Memory lifecycle C, AC2: the human's explicit yes to a `retire?`
    suggestion. 404 unknown id; 409 if the row is not confirmed (retirement
    is for ACTIVE rules — an unconfirmed proposal has `reject` for that job).
    Idempotent: retiring an already-archived row returns
    ``{"ok": True, "already_archived": True}`` rather than an error, since a
    dismissed-then-retried client action should never surface as a failure."""
    store = _store(request)
    m = await store.find_memory(mem_id)
    if not m:
        raise HTTPException(status_code=404, detail=f"learning {mem_id!r} not found")
    if m.get("archived"):
        return {"ok": True, "id": m["id"], "already_archived": True}
    if not m.get("confirmed"):
        raise HTTPException(
            status_code=409,
            detail="only a confirmed (active) rule can be retired — "
                   "reject the pending proposal instead")
    from ..learning import LearningQueue
    await LearningQueue(store).retire(m["id"])
    return {"ok": True, "id": m["id"]}


@app.post("/api/learnings/{mem_id}/restore")
async def restore_learning(mem_id: str, request: Request) -> dict[str, Any]:
    """Memory lifecycle C part B: the Rules/Skills UI's triage action — a
    human's explicit undo of an archive, whatever produced it (the 45-day
    sweep, a supersede-on-confirm, or a manual retire). 404 unknown id;
    idempotent on a row that is already live (``already_active: True``, 200
    — the same double-click contract `retire_learning` chose, so a stale
    button never surfaces as a failure)."""
    store = _store(request)
    m = await store.find_memory(mem_id)
    if not m:
        raise HTTPException(status_code=404, detail=f"learning {mem_id!r} not found")
    if not m.get("archived"):
        return {"ok": True, "id": m["id"], "already_active": True}
    await store.unarchive_memory(m["id"])
    return {"ok": True, "id": m["id"]}


@app.get("/api/memories/quarantine")
async def quarantine_counts(request: Request) -> dict[str, int]:
    """Per-panel quarantined row counts (P1 brain hygiene) — an honest
    footer, not a changed list shape. `/api/rules` and `/api/skills` keep
    returning a bare list; this is a NEW endpoint so their response shape
    stays untouched.

    ``total`` is deliberately the ALL-TYPES quarantined count, not
    ``rules + skills`` — the memories table has more types than the four the
    Rules/Skills panels cover (e.g. proposals), and a row of one of those can
    be quarantined without ever surfacing in either panel. So
    ``total >= rules + skills`` is expected, not a double-count bug (round-3
    review advisory 2): the Learnings footer is deliberately the grand total
    across every type, while ``rules``/``skills`` are the two panel subsets.
    """
    store = _store(request)
    from ..learning import TYPE_ANTI_PATTERN, TYPE_FACT, TYPE_RULE, TYPE_SKILL
    rules = (await store.count_quarantined(mem_type=TYPE_RULE)
             + await store.count_quarantined(mem_type=TYPE_ANTI_PATTERN))
    skills = (await store.count_quarantined(mem_type=TYPE_SKILL)
              + await store.count_quarantined(mem_type=TYPE_FACT))
    all_types_total = await store.count_quarantined()
    return {"rules": rules, "skills": skills, "learnings": all_types_total,
            "total": all_types_total}


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


@app.get("/api/version")
async def show_version() -> dict[str, Any]:
    """The running `nh` version, and whether the browser Updates panel may
    print a pip command for it.

    The board runs in two places. Inside the desktop shell the version arrives
    over the preload bridge as ``window.nhDesktop.version``; in a plain browser
    there is no bridge, so Settings > Updates printed "You are running no_human
    unknown in a browser". The server always knows — it IS the installed
    package — so it says so. Deliberately NOT folded into /api/config: a version
    is not configuration, and that payload is already broader than it should be.

    ``no_human.__version__`` is the same string ``nh --version`` prints and the
    same one the update check compares against, so all three agree by
    construction.

    ``dist_name``/``published`` let the browser panel derive its upgrade
    instruction from the real distribution channel instead of hardcoding a
    package name that may not exist there yet: ``published`` is fail-closed
    (``updates.is_published()`` never raises and defaults to False), so an
    absent or unreadable cache reads as "not provably published", never as a
    false "yes".
    """
    from .. import __version__, updates

    return {
        "version": __version__,
        "dist_name": updates.DIST_NAME,
        "published": updates.is_published(),
    }


@app.get("/api/integrations")
async def list_integrations_endpoint(request: Request) -> dict[str, Any]:
    """Status of every integration (configured + kind; healthy is null until a
    `test` is run), PLUS its `fields` array so the UI can render a settings
    form. Never returns a secret — `fields` carries only `set: bool`."""
    from ..integrations import integration_fields, list_integrations_with_ambient
    cfg = request.app.state.config
    out = []
    # The ambient overlay can shell out to `gh`/`git` (subprocess.run with a
    # multi-second timeout) — the same asyncio.to_thread discipline the rest
    # of this file uses for blocking work, so a slow/hanging CLI probe never
    # freezes the single-threaded event loop (SSE, task list, every request).
    statuses = await asyncio.to_thread(list_integrations_with_ambient, cfg.data)
    for s in statuses:
        d = asdict(s)
        d["fields"] = integration_fields(s.name, cfg.data)
        out.append(d)
    return {"integrations": out}


@app.get("/api/integrations/setup")
async def integration_setup_specs(request: Request) -> dict[str, Any]:
    """What the onboarding "Connect your tools" step renders itself from.

    One entry per block under ``DEFAULT_CONFIG["integrations"]`` — DISCOVERED,
    not a list of names in the UI, so adding a sixth block makes a sixth card
    appear with no frontend change. Carries the non-secret current values, the
    on/off switch, and the NAMES of the ~/.no_human/.env variables each
    integration's credential needs (plus whether each is set) — never a secret
    value, and never a field the wizard is allowed to write a secret into."""
    from ..integrations import setup_specs
    cfg = request.app.state.config
    return {"integrations": setup_specs(cfg.data)}


@app.put("/api/integrations/{name}/setup")
async def save_integration_setup(
    name: str, body: IntegrationSetupRequest, request: Request
) -> dict[str, Any]:
    """Persist one integration's NON-SECRET onboarding settings to config.yaml.

    Distinct from ``/api/integrations/{name}/config`` on purpose: that route
    can route a field to ~/.no_human/.env, this one writes config.yaml ONLY
    and refuses (422) any field that reads as a credential, so the wizard can
    never put a token in a world-readable file. Same local-origin guard as
    every other config write."""
    from ..integrations import apply_setup

    _require_local_origin(request, writing=True)
    try:
        spec = await asyncio.to_thread(apply_setup, name, dict(body.values))
    except ValueError as exc:
        # Unknown integration/field and "that's a credential" are both the
        # caller's mistake; 422 carries the message the UI shows verbatim.
        raise HTTPException(status_code=422, detail=str(exc))

    from ..config import CONFIG_PATH, load_config
    refreshed = load_config(CONFIG_PATH)
    request.app.state.config.data = refreshed.data
    return spec


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
        # save_integration_config overlays an ambient probe that shells out
        # (subprocess.run) — offload it exactly like the list endpoint above,
        # or a settings save freezes the single-threaded loop for up to 2s.
        status = await asyncio.to_thread(save_integration_config, name, body.fields)
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


@app.put("/api/telemetry/consent")
async def save_telemetry_consent(
    body: TelemetryConsentRequest, request: Request
) -> dict[str, Any]:
    """Settings > Usage insights: persist `telemetry.enabled` to config.yaml.

    On FIRST enable, mints the anonymous `telemetry.instance_id` (uuid4)
    HERE, server-side, in the same write — the id never comes from the
    browser. Off by default; turning it off writes `enabled: false` and
    leaves the id in place (so re-enabling keeps one stable anonymous id
    rather than manufacturing a fresh "new install" every toggle)."""
    import uuid

    from ..integrations import _write_config_values

    _require_local_origin(request, writing=True)
    from ..config import CONFIG_PATH, load_config

    updates: dict[str, Any] = {"telemetry.enabled": bool(body.enabled)}
    current = load_config(CONFIG_PATH).data.get("telemetry") or {}
    if body.enabled and not str(current.get("instance_id") or "").strip():
        updates["telemetry.instance_id"] = str(uuid.uuid4())
    await asyncio.to_thread(_write_config_values, CONFIG_PATH, updates)

    refreshed = load_config(CONFIG_PATH)
    request.app.state.config.data = refreshed.data
    # Recompute the CSP now (same builder the lifespan uses at app start), so
    # the widened/strict header tracks consent without waiting for a restart.
    request.app.state.csp = _build_csp(refreshed.data)
    tel = copy.deepcopy(refreshed.data.get("telemetry") or {})
    # Replay/init runs at page bootstrap, so the browser reloads to apply.
    return {"telemetry": _scrub_secrets(tel), "reload_required": True}


async def _attach_imported(
    request: Request, source: str, briefs: list[dict[str, Any]]
) -> list[TrackerIssueOut]:
    """SCRUM-18 — the accidental re-import trap, for either tracker.

    ONE local-store read (never a per-row store or tracker call) building an
    external_id -> [tasks] index, then an `imported` block on any row that
    already has a board task. A deleted board task simply isn't in the
    projection any more, so its ticket goes back to showing no chip — no stale
    reference is fabricated.

    `source` scopes the read, and that scoping is load-bearing now that two
    trackers are listed: dedupe keys on (source, external_id), so a Jira NO-1
    and a Linear NO-1 are different tickets and neither may claim the other's
    task. SCRUM-54: the narrow (external_id, id, status, created_at) projection,
    not a full `list_tasks()` hydration of every task just to read four fields.
    """
    imported_rows = await _store(request).list_imported_tasks(source)
    by_ext: dict[str, list] = {}
    for t in imported_rows:
        by_ext.setdefault(t.external_id, []).append(t)
    out = []
    for brief in briefs:
        row = TrackerIssueOut(**brief)
        matches = by_ext.get(brief.get("key"))
        if matches:
            # Same "latest task per external_id" definition as the sync
            # (jira_poll.sync_statuses): newest (created_at, id) — an older
            # import merely touched later must not win the chip.
            latest = max(matches, key=lambda t: (t.created_at, t.id))
            row.imported = ImportedInfo(
                task_id=latest.id, status=latest.status, count=len(matches),
            )
        out.append(row)
    return out


@app.get("/api/integrations/jira/issues", response_model=list[TrackerIssueOut])
async def jira_issues_endpoint(
    q: str = "", limit: int = 20, request: Request = None
) -> list[TrackerIssueOut]:
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
        # object) — a short, tokenless detail only, and only the exception's
        # type name is ever logged.
        log.warning("jira issue search failed: %s", type(exc).__name__)
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (401, 403):
            detail = (
                "Jira API token expired or invalid. Verify or rotate the "
                "token under Settings > Integrations."
            )
        else:
            detail = "Jira search failed — check the site/project configuration."
        raise HTTPException(status_code=502, detail=detail)
    # SCRUM-18 — accidental re-import trap: one local-store read (no per-row
    # Jira calls) building an external_id -> [tasks] index, then attach an
    # `imported` block to any issue that already has a board task. A deleted
    # board task simply isn't in this projection any more, so its ticket goes
    # back to showing no chip — no stale reference is fabricated.
    # SCRUM-54: a narrow (external_id, id, status, created_at) projection —
    # filtered to source='jira' AND external_id IS NOT NULL in SQL — replaces
    # the old list_tasks() full-Task hydration of every task in the store.
    return await _attach_imported(
        request, "jira", [adapter.issue_brief(i) for i in issues])


@app.get("/api/integrations/jira/issues/{key}", response_model=TrackerIssueOut)
async def jira_issue_detail_endpoint(key: str, request: Request) -> TrackerIssueOut:
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
    return TrackerIssueOut(**adapter.issue_detail(issue))


# --------------------------------------------------------------------------- #
# Linear — the SAME two routes, against the SAME adapter the poller uses.       #
#                                                                              #
# These did not exist, and the Backlog page told the operator why in a sentence #
# that was not true: "the Linear side has no issue listing yet". It has had one #
# the whole time — `LinearAdapter.search()` is a paginating GraphQL listing     #
# with a Relay cursor and a page bound. What was missing was only the HTTP      #
# route between it and the page. A UI that explains a gap with a fact about the #
# code has to be right about the code, so this closes the gap rather than       #
# rewording the sentence.                                                       #
# --------------------------------------------------------------------------- #

def _linear_adapter(request: Request):
    """The configured adapter, or a 503 that says what to fix.

    LINEAR_API_KEY is loaded from ~/.no_human/.env on demand — the B1 pattern
    the Jira routes above use, and for the same reason: only `nh serve`'s poller
    loads it at startup, so under `nh start` (the board) a perfectly configured
    integration reported "not configured" until the key was read here.
    """
    from ..config import load_env_var
    from ..intake.linear import LinearAdapter

    load_env_var("LINEAR_API_KEY")
    adapter = LinearAdapter(request.app.state.config.data)
    if not adapter.configured:
        raise HTTPException(
            status_code=503,
            detail="Linear is not configured — add it under Settings > Integrations.",
        )
    return adapter


def _linear_failure(exc: Exception, what: str) -> HTTPException:
    """One 502 for every Linear failure mode, with a tokenless detail.

    Linear does not classify by HTTP status — field errors arrive at 200, auth
    failure at 401, throttling at 400 — so the adapter's exception TYPE is the
    classification, not the status code. Only the exception's type name is ever
    logged: the message can quote the request, which carries the API key.
    """
    from ..intake.linear import LinearAuthError, LinearConfigError, LinearRateLimited

    log.warning("linear %s failed: %s", what, type(exc).__name__)
    if isinstance(exc, LinearConfigError):
        # The adapter builds this one itself, out of the operator's own config
        # and names the API returned — never out of a request — so it is the
        # one Linear failure whose message can be shown verbatim, and it is
        # the only one that tells the operator what to change. 503, not 502:
        # nothing is wrong upstream, the setting is wrong here.
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, LinearAuthError):
        detail = (
            "Linear API key rejected. Verify or rotate the key under "
            "Settings > Integrations."
        )
    elif isinstance(exc, LinearRateLimited):
        detail = "Linear is rate-limiting this workspace — try again in a minute."
    else:
        detail = "Linear lookup failed — check the team key and state types."
    return HTTPException(status_code=502, detail=detail)


@app.get("/api/integrations/linear/issues", response_model=list[TrackerIssueOut])
async def linear_issues_endpoint(
    q: str = "", limit: int = 20, request: Request = None
) -> list[TrackerIssueOut]:
    """Browse/pick over the configured Linear team's intake scope — the Linear
    half of the Backlog page's list. Never creates a task; POST /api/tasks
    (with source="linear") stays the one create path."""
    from ..intake.linear import LinearError

    adapter = _linear_adapter(request)
    limit = max(1, min(limit, 50))
    try:
        issues = await asyncio.to_thread(adapter.search_text, q, limit)
    except (LinearError, httpx.HTTPError) as exc:
        raise _linear_failure(exc, "issue search")
    return await _attach_imported(
        request, "linear", [adapter.issue_brief(i) for i in issues])


@app.get("/api/integrations/linear/issues/{key}", response_model=TrackerIssueOut)
async def linear_issue_detail_endpoint(key: str, request: Request) -> TrackerIssueOut:
    """ONE issue by its identifier ("NO-1"). Same contract as the Jira detail
    route so the page has one code path per row, whichever tracker it came
    from. 404 when the identifier is not in the configured intake scope —
    saying "not found" is honest; inventing an empty row is not."""
    from ..intake.linear import LinearError

    adapter = _linear_adapter(request)
    try:
        issue = await asyncio.to_thread(adapter.get_issue, key)
    except (LinearError, httpx.HTTPError) as exc:
        raise _linear_failure(exc, "issue detail fetch")
    if issue is None:
        raise HTTPException(
            status_code=404,
            detail=f"{key} is not in the configured Linear team's open issues.",
        )
    return TrackerIssueOut(**adapter.issue_detail(issue))


# --------------------------------------------------------------------------- #
# Onboarding wizard (web first-run). Reuses the existing onboard/history/      #
# learning logic — no parallel machinery.                                      #
#                                                                              #
# The derive/prove split is preserved, but BOTH halves are now reachable from  #
# the app: `/repos/onboard` derives (fast, one click) and `/repos/prove`       #
# streams a REAL run of the derived commands (`OnboardEngine`, the same engine #
# `nh onboard` drives), then `/repos/confirm` applies the same human gate the  #
# CLI applies (`onboard.confirm_profile`).                                     #
#                                                                              #
# Why this matters, stated so it is not re-broken: without a proven test       #
# command a task still RUNS — it just runs with no test command, so            #
# `runner.run_tests` falls back to `detect_command` and, when that finds        #
# nothing, reports "no tests run" as a non-failure. The PR still opens. The     #
# missing proof does not block the product; it hollows out the evidence the    #
# product's review gate is supposed to stand on. Proving in the wizard is      #
# about EVIDENCE, not about unblocking anyone.                                 #
# --------------------------------------------------------------------------- #

class RepoDetectRequest(BaseModel):
    root: str | None = None  # defaults to ~/git

class RepoOnboardRequest(BaseModel):
    repo_path: str

class RepoProveRequest(BaseModel):
    """Prove a repo's commands by RUNNING them. The optional command fields are
    the human's correction after a failed attempt; each REPLACES that kind's
    derived candidates so we prove exactly the string the human typed."""
    repo_path: str
    test_cmd: str | None = None
    install_cmd: str | None = None
    lint_cmd: str | None = None
    timeout: int = 1800

class RepoConfirmRequest(BaseModel):
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


@app.get("/api/repos/discover")
async def discover_repositories(
    request: Request,
    limit: int | None = Query(default=None, ge=1, le=1000),
) -> dict[str, Any]:
    """Find the user's repositories so onboarding and the composer can offer a
    list instead of demanding a typed path.

    There is deliberately no caller-supplied root: the scan is bound to the
    process's own home directory plus whatever the operator put in
    ``onboarding.extra_scan_roots``. A root parameter would turn a localhost
    convenience endpoint into an arbitrary-filesystem scanner, and the typed
    path field (which stays) already covers "somewhere else".
    """
    from ..repo_discovery import DEFAULT_MAX_RESULTS, discover_repos

    ob = (request.app.state.config.data.get("onboarding") or {})
    extra = ob.get("extra_scan_roots") or []
    if isinstance(extra, str):
        extra = [extra]
    return await asyncio.to_thread(
        discover_repos,
        extra_roots=list(extra),
        max_results=limit if limit is not None else DEFAULT_MAX_RESULTS,
    )


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
        proven={},          # unproven — prove via /repos/prove (or `nh onboard`)
        confirmed=False,
        notes="derived in onboarding wizard (unproven — prove it to give the "
              "review gate a test command to run)",
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


def _profile_readiness(prof: Any) -> dict[str, Any]:
    """The one shape the whole app uses to describe how far a repo profile got
    up the trust ladder. ``is_usable`` is READ from ``ProjectProfile`` — never
    recomputed here, so no surface can disagree with the orchestrator's gate."""
    return {
        "repo_path": prof.repo_path,
        "name": (prof.repo_path or "").rstrip("/").rsplit("/", 1)[-1],
        "ecosystem": prof.ecosystem,
        "install_cmd": prof.install_cmd,
        "test_cmd": prof.test_cmd,
        "lint_cmd": prof.lint_cmd,
        "proven": dict(prof.proven or {}),
        "test_proven": bool((prof.proven or {}).get("test_cmd")),
        "confirmed": bool(prof.confirmed),
        "is_usable": bool(prof.is_usable),
    }


@app.get("/api/onboarding/readiness")
async def onboarding_readiness(request: Request) -> dict[str, Any]:
    """Which onboarded repos can back a task with REAL test evidence.

    This is what the summary step and the board banner read, so neither can
    claim "Ready." while every profile is unproven. A repo missing from
    ``usable`` is not blocked — its tasks will run — but its review gate will
    have no test command to execute, which is the thing worth saying out loud.
    """
    store = _store(request)
    try:
        rows = await store.list_profiles()
    except Exception:  # noqa: BLE001 — table may not exist yet
        rows = []
    from ..profile import ProjectProfile
    repos = [_profile_readiness(ProjectProfile.from_dict(json.loads(r["data"])))
             for r in rows if r.get("data")]
    usable = [r for r in repos if r["is_usable"]]
    return {
        "repos": repos,
        "total": len(repos),
        "usable": len(usable),
        "needs_proving": [r for r in repos if not r["is_usable"]],
        "first_usable": usable[0]["repo_path"] if usable else None,
    }


@app.post("/api/onboarding/repos/prove")
async def onboarding_prove_repo(body: RepoProveRequest, request: Request):
    """PROVE a repo's derived commands by actually RUNNING them, streaming the
    real output back as SSE so the user watches the thing that decides.

    This is the same `OnboardEngine` `nh onboard` drives — not a second
    implementation — so the command proven here is byte-for-byte the command
    `runner.run_tests` executes for the orchestrator later. Nothing in this
    endpoint can create a proof: it only reports the exit status of a real
    subprocess, and it always persists the profile UNCONFIRMED. Confirming is a
    separate human act (`/repos/confirm`).

    A failing command is a legitimate outcome, not an error: the stream reports
    it with its output and the caller may re-POST with a corrected `test_cmd`.
    """
    store = _store(request)
    config = request.app.state.config
    repo = Path(body.repo_path).expanduser().resolve()
    if not repo.is_dir() or not (repo / ".git").exists():
        raise HTTPException(422, f"{body.repo_path!r} is not a git repository")

    from ..onboard import DeclarationDeriver, OnboardEngine

    github_hosts = (config.data.get("git") or {}).get("github_hosts") or ["github.com"]
    overrides = {"test": body.test_cmd or "", "install": body.install_cmd or "",
                 "lint": body.lint_cmd or ""}
    timeout = max(30, min(int(body.timeout or 1800), 7200))

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _emit(frame: dict[str, Any] | None) -> None:
        # Called from the prove worker thread (each output line) as well as from
        # the loop, so it must hop threads explicitly.
        loop.call_soon_threadsafe(queue.put_nowait, frame)

    async def _run_prove() -> None:
        try:
            engine = OnboardEngine(
                DeclarationDeriver(), prove_timeout=timeout,
                github_hosts=github_hosts, on_event=_emit,
            )
            result = await engine.onboard(repo, overrides=overrides)
            prof = result.profile
            # Never inherit an earlier confirm: the command may have changed, so
            # the human re-confirms against THIS evidence.
            prof.confirmed = False
            await store.upsert_profile(prof)
            try:
                prof.save()
            except OSError as exc:
                log.warning("could not write project.yml for %s: %s", repo, exc)
            _emit({
                "kind": "done",
                **_profile_readiness(prof),
                "proofs": [
                    {"kind": p.kind, "command": p.command, "ok": p.ok,
                     "exit_code": p.exit_code, "output": (p.output or "")[-4000:]}
                    for p in result.proofs
                ],
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("prove failed for %s: %s", repo, type(exc).__name__)
            _emit({"kind": "error", "text": f"{type(exc).__name__}: {exc}"})
        finally:
            _emit(None)  # sentinel

    async def _generate():
        task = asyncio.create_task(_run_prove())
        started = time.monotonic()
        try:
            while True:
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=10)
                except asyncio.TimeoutError:
                    # A quiet suite is normal (compiling, installing). Say so
                    # with an elapsed count rather than leaving a dead spinner.
                    yield ("data: " + json.dumps({
                        "kind": "heartbeat",
                        "elapsed": int(time.monotonic() - started),
                    }) + "\n\n")
                    continue
                if frame is None:
                    yield "data: {\"kind\": \"stream_end\"}\n\n"
                    return
                yield f"data: {json.dumps(frame)}\n\n"
        finally:
            task.cancel()

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/onboarding/repos/confirm")
async def onboarding_confirm_repo(
    body: RepoConfirmRequest, request: Request
) -> dict[str, Any]:
    """The human gate, from the app: mark a PROVEN profile confirmed.

    Delegates the decision to ``onboard.confirm_profile`` — the same function
    `nh onboard --confirm` calls — so the GUI can never confirm something the
    CLI would refuse. An unproven profile is rejected here; the remedy is to
    prove it, never to relax this.
    """
    store = _store(request)
    repo = Path(body.repo_path).expanduser().resolve()

    from ..onboard import ProfileNotProven, confirm_profile
    from ..profile import ProjectProfile

    prof = await store.get_profile(str(repo)) or ProjectProfile.load(repo)
    if prof is None:
        raise HTTPException(404, f"no profile for {body.repo_path!r} — onboard it first")
    try:
        confirm_profile(prof)
    except ProfileNotProven as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        prof.save()
    except OSError as exc:
        log.warning("could not write project.yml for %s: %s", repo, exc)
    await store.upsert_profile(prof)
    return {"ok": True, **_profile_readiness(prof)}


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
    from ..learning.pii import contains_pii
    skills_added = 0
    for s in await asyncio.to_thread(discover_skills):
        # Same personal-data gate as the mined findings — a skill's name or
        # description is user-authored text and reaches the same queue.
        if contains_pii(s.name, s.description or "") is not None:
            continue
        mid = await store.add_memory(
            mem_type="skill", title=s.name, content=s.description or s.name,
            tags=["skill", "claude_code"], source="proposed", confirmed=False,
            dedupe_key=f"skill:{s.name}",
        )
        if mid:
            skills_added += 1
            proposals.append({"id": mid, "category": "skill", "title": s.name,
                              "content": s.description or s.name, "importance": "med"})

    # NOTHING IS PRE-SELECTED. A real user was shown their own home address and
    # phone number already TICKED for confirmation as standing guidance — one
    # click from becoming an active rule. Confirmation is opt-in per memory:
    # the server states the default explicitly rather than leaving it to the
    # client to decide, so any client (SPA, future CLI/TUI, a third-party one)
    # inherits opt-in rather than re-inventing pre-ticking.
    for p in proposals:
        p["selected"] = False

    return {"available": True, "proposed": result.proposed + skills_added,
            "duplicates": result.duplicates, "messages": messages,
            "sources": sources, "skills": skills_added,
            "dropped_pii": result.dropped_pii,
            "default_selected": False,
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


@app.post("/api/onboarding/reset")
async def onboarding_reset(request: Request) -> dict[str, Any]:
    """Show the setup wizard again. Clears `completed` and NOTHING else.

    There was no way back into onboarding: `completed` was written True in one
    place and False nowhere, so a user who blew through the eight steps (none of
    which gate) and landed in a wrong state — no proven repo, no projects,
    history never scanned — had one route, hand-editing config.yaml and
    restarting the server. The wizard is the screen that fixes all of those and
    it removed itself.

    The reset patch is deliberately one key (`completed`): repos, profiles,
    projects, confirmed rules, docs and the persisted `team` value (config only,
    not a wizard step — the step itself left the free-tier wizard) all survive
    the reset itself, untouched in memory and on disk. But the wizard FORM does
    not read any of that back: `Onboarding.jsx` starts repo, docs and project
    selections as empty React state and posts `team: null`, so if the user
    re-completes the wizard afterwards, `onboarding_complete` overwrites
    `team`/`repos`/`docs` with whatever the fresh form holds. The reset alone
    loses nothing; a full re-completion afterwards can.

    The board reads this flag once, at load: the desktop's File → "Re-run Setup…"
    resets and reloads the window; a caller hitting this endpoint on its own has
    to reload the board itself.
    """
    ob = _persist_onboarding(request.app.state.config, {"completed": False})
    return {"completed": bool(ob.get("completed")), **ob}


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

if (_WEB_DIST / "index.html").is_file():
    app.mount("/assets", StaticFiles(directory=str(_WEB_DIST / "assets")), name="assets")

    @app.get("/", include_in_schema=False)
    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str = "") -> FileResponse:
        # Never intercept /api/ or /ws paths — those are backend routes.
        # If they reach here, it means the route doesn't exist (404).
        if path.startswith("api/") or path.startswith("ws"):
            return PlainTextResponse(f"Not found: /{path}", status_code=404)
        # Vite copies `web/public/` to the ROOT of dist, not under /assets, so
        # a root-level static file (the brand mark, robots.txt, a manifest) is
        # outside the only mounted directory and would fall through to the app
        # shell. It did: the installed app answered /nh-mark-64.png with 601
        # bytes of index.html, so its own favicon was broken for every user
        # while every content check passed — the file was present, built and
        # bundled, and simply unreachable.
        #
        # resolve() then a parent check, because `path` is caller-controlled:
        # without it, `../../etc/passwd` reads outside the board directory.
        if path:
            candidate = (_WEB_DIST / path).resolve()
            try:
                inside = candidate.is_relative_to(_WEB_DIST.resolve())
            except AttributeError:                       # py<3.9
                inside = str(candidate).startswith(str(_WEB_DIST.resolve()))
            if inside and candidate.is_file():
                return FileResponse(str(candidate))
        # no-cache: index.html references content-hashed assets; without an
        # explicit header Chromium's HEURISTIC freshness serves a stale app
        # shell after every deploy (found live: the Electron shell ran a
        # bundle two deploys old while the e2e gate — which spins its own
        # static server — stayed green). Hashed /assets remain long-cacheable.
        return FileResponse(str(_WEB_DIST / "index.html"),
                            headers={"Cache-Control": "no-cache"})

else:
    # The board is missing. Before this branch existed the server simply had no
    # "/" route, so `nh start` — which README calls the primary entrypoint —
    # answered the browser with FastAPI's bare `{"detail":"Not Found"}` and the
    # user had no way to tell a broken install from a broken app. The API and
    # the worker are genuinely fine in this state, so this is not a hard
    # failure; it is a route that says which of the two situations it is and
    # what to do about it.
    log.warning(
        "board not found at %s — serving the API only. `nh start` will not "
        "render a UI. If this is a source checkout, build it with "
        "`cd web && npm install && npm run build`.", _WEB_DIST,
    )

    _NO_BOARD_MESSAGE = (
        "no_human: the web board is not installed.\n"
        "\n"
        f"Looked for index.html at: {_WEB_DIST}\n"
        "\n"
        "The API and the task worker are running normally — only the UI is\n"
        "missing, so the CLI works: `nh task`, `nh status`, `nh logs`,\n"
        "`nh approve`.\n"
        "\n"
        "To get the board:\n"
        "  * source checkout -> cd web && npm install && npm run build\n"
        "  * pip/uv install  -> this is a packaging bug, please report it;\n"
        "    a released wheel always ships the board.\n"
    )

    @app.get("/", include_in_schema=False)
    @app.get("/{path:path}", include_in_schema=False)
    async def spa_missing(path: str = "") -> PlainTextResponse:
        # Identical carve-out to the served case: /api/ and /ws are backend
        # routes, and a genuine 404 there must stay a plain 404 rather than be
        # answered with the board-missing notice.
        if path.startswith("api/") or path.startswith("ws"):
            return PlainTextResponse(f"Not found: /{path}", status_code=404)
        # 503, not 404: the resource is meant to exist and the deployment is
        # incomplete. A 404 reads as "wrong URL" and sends the user hunting.
        return PlainTextResponse(_NO_BOARD_MESSAGE, status_code=503)
