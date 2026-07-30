"""Phase 7.3/7.4: the concurrent task scheduler.

A single-event-loop pool that drains the SQLite queue into at most
``max_workers`` concurrent ``run_task`` coroutines. Concurrency is real because
the two long phases yield: the Agent SDK session is async, and the orchestrator
offloads the blocking test subprocess to a thread. Each task runs in its own git
worktree (``isolation.enabled``, on by default), so same-repo tasks don't
collide. Isolation and parallelism are separate switches: isolation is the
default for a single task, and a hard requirement for a pool of more than one.

Two coordination rules:
  - **No double-dispatch.** A task id is reserved in ``_inflight`` synchronously
    before its coroutine is scheduled, so the next tick won't re-claim it (even
    though its DB status becomes IMPLEMENTING mid-run).
  - **Shared-quota gate (7.4).** All workers share one subscription. When any
    task parks PAUSED_QUOTA, the pool stops dispatching until the reset time —
    one worker hitting the limit pauses the whole pool, not just itself.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Awaitable, Callable

from ..config import parallelism_enabled, worktree_isolation_enabled
from .db import Store
from .events import EventPersister
from .task import TaskStatus

log = logging.getLogger("no_human.scheduler")

# Tasks the scheduler may pick up: freshly created, or flipped back to
# IMPLEMENTING by the WakeWatcher / `nh reply` resume. IMPLEMENTING first —
# WIP-first: resumed work carries sunk cost and a waiting operator, and
# pending-first starved three budget-raised resumes behind every newly
# imported ticket on a one-worker pool (live, 2026-07-24).
_CLAIMABLE = (TaskStatus.IMPLEMENTING, TaskStatus.PENDING)


def resolve_max_workers(
    config: dict, *, override: int | None = None,
) -> tuple[int, str | None]:
    """Effective pool size, plus a warning when a request for >1 is refused.

    Two independent switches (see ``config.worktree_isolation_enabled`` /
    ``config.parallelism_enabled``):

    - ``isolation.enabled`` — each task gets its own worktree. On by default,
      so the width below is normally allowed.
    - ``concurrency.enabled`` — more than one task at a time. Off by default.

    The pool must never be wider than the isolation allows. With ``enabled:
    false, max_workers: 2`` the server once announced "2 worker(s) · concurrent"
    while two tasks ran in the SAME checkout, stomping each other's branch and
    index. The CLI and the server lifespan both resolve the width here; they
    used to compute it separately, which is how the announcement and the real
    pool were free to disagree.
    """
    conc = config.get("concurrency", {}) or {}
    requested = max(1, int(override or conc.get("max_workers", 1) or 1))
    if requested > 1:
        if not worktree_isolation_enabled(config):
            return 1, (
                f"isolation.enabled is false - running 1 worker, not {requested}. "
                "Parallel tasks would share one checkout, stomping each other's "
                "branch and index. Re-enable isolation.enabled to run them in "
                "parallel."
            )
        if not parallelism_enabled(config):
            return 1, (
                f"concurrency.enabled is false - running 1 worker, not {requested}. "
                "Set concurrency.enabled: true to run them in parallel."
            )
    return requested, None


def resolve_serve_pool(
    config: dict, *, cli_workers: int | None,
) -> tuple[int, bool, str | None]:
    """``nh serve``'s decision: an explicit ``--max-workers`` enables the pool
    for THIS invocation only (the config default on disk stays whatever it was).
    Returns ``(workers, enabled, error)``; ``error is not None`` means: don't
    serve, print it and exit.

    - ``cli_workers`` given and < 1  -> ``(0, False, "<validation message>")``.
    - ``cli_workers`` given and >= 1 -> the flag buys parallelism:
      ``(cli_workers, True, None)`` even if ``concurrency.enabled`` is false in
      config. It does NOT buy isolation — that is a separate switch, and a flag
      must not override an operator who explicitly turned isolation off.
    - ``cli_workers`` is None        -> refuse when ``concurrency.enabled`` is
      false; otherwise use ``concurrency.max_workers`` (default 2, matching
      serve()'s historical default — NOT ``resolve_max_workers``'s default of 1,
      which is for the override-clamp case, not "no flag given at all").

    Isolation is a DEFAULT for one task and a REQUIREMENT for many: any pool
    wider than one worker is refused outright when ``isolation.enabled`` is
    false, rather than quietly downgraded, because N workers in one checkout
    is the exact collision the isolation exists to prevent.
    """
    conc = config.get("concurrency", {}) or {}
    isolated = worktree_isolation_enabled(config)

    def _no_isolation(width: int) -> str:
        return (
            f"isolation.enabled is false - refusing to serve {width} workers. "
            "They would share one checkout and stomp each other's branch and "
            "index. Set isolation.enabled: true in ~/.no_human/config.yaml, or "
            "serve a single worker."
        )

    if cli_workers is not None:
        if cli_workers < 1:
            return 0, False, (
                f"--max-workers must be a positive integer, got {cli_workers}."
            )
        if cli_workers > 1 and not isolated:
            return 0, False, _no_isolation(cli_workers)
        return cli_workers, True, None

    if not parallelism_enabled(config):
        return 0, False, (
            "concurrency.enabled is false - set it in ~/.no_human/config.yaml "
            "to run the pool, or pass --max-workers N to enable it for this "
            "run. Refusing to serve a pool that was not asked for."
        )
    workers = int(conc.get("max_workers", 2) or 2)
    if workers > 1 and not isolated:
        return 0, False, _no_isolation(workers)
    return workers, True, None


def bounded_xdist_workers(
    max_workers: int, cpu_count: int, existing: str | None,
) -> str | None:
    """The value to set for PYTEST_XDIST_AUTO_NUM_WORKERS, or None to leave it.

    N parallel tasks each running `pytest -n auto` spawn N×cpu workers on cpu
    cores (2026-07-11: 3×12 on 12 → every run timed out). Bound each task's
    auto count to cpu//max_workers. Serial mode (max_workers≤1) and an
    already-set value are left untouched — never lower an explicit choice."""
    if max_workers <= 1 or existing:
        return None
    return str(max(1, (cpu_count or 2) // max_workers))


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _summarize_event(event: dict) -> str | None:
    """Extract a short human-readable summary from an event dict.

    Returns None for events that are not worth showing as a live status.
    """
    kind = event.get("kind", "")
    text = event.get("text", "")

    if kind == "tool_use":
        tool = event.get("tool_name") or text.split(" ", 1)[0] or "tool"
        inp = event.get("tool_input") or {}
        path = inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or ""
        basename = path.rsplit("/", 1)[-1] if path else ""
        if tool in ("Read", "View"):
            return f"reading {basename}" if basename else "reading file"
        if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            return f"editing {basename}" if basename else "editing file"
        if tool in ("Bash", "Terminal"):
            cmd = inp.get("command", "")[:60]
            return f"running: {cmd}" if cmd else "running command"
        if tool == "Search":
            return f"searching: {inp.get('query', '')[:50]}"
        return f"{tool.lower()} {basename}".strip()

    if kind == "state":
        return text  # e.g. "implementing", "reviewing"
    if kind == "commit":
        return "committing changes"
    if kind in ("tests", "lint"):
        return f"running {kind}"
    if kind in ("review_start", "review"):
        return "reviewing code"
    if kind == "attempt_start":
        return text  # e.g. "attempt 1/3"
    if kind == "context_gather" or kind == "context":
        return "gathering context"
    if kind == "supervisor":
        return "supervisor check"
    if kind == "decompose":
        return "decomposing into sub-tasks"
    return None


class Scheduler:
    def __init__(
        self,
        store: Store,
        orchestrator_factory: Callable[..., object],
        *,
        max_workers: int = 2,
        wake_watcher: object | None = None,
        on_event: Callable[[str, str], None] | None = None,
        reanalysis_job: ReanalysisJob | None = None,
        wiki_refresh_job: "WikiRefreshJob | None" = None,
        config: dict | None = None,
    ):
        self.store = store
        self.factory = orchestrator_factory
        self.max_workers = max(1, int(max_workers))
        self.wake = wake_watcher
        self._inflight: set[str] = set()
        self._on_event = on_event or (lambda kind, text: None)
        self._quota_cooldown_until: datetime | None = None
        self.reanalysis = reanalysis_job
        self.wiki_refresh = wiki_refresh_job
        self._config = config or {}
        # Per-task event log: task_id -> deque of {ts, source, kind, text, ...}
        self._event_log: dict[str, deque] = {}
        self._MAX_EVENTS = 200
        # Events are flushed to SQLite while the run is live, not just at the
        # end: a crash used to lose the entire history, and the in-memory buffer
        # above is capped, so a chatty run silently dropped its earliest events.
        self._EVENT_FLUSH_INTERVAL = 2.0
        # Phase 4a: SSE — per-task notify so streaming clients wake on new events.
        self._event_notify: dict[str, asyncio.Event] = {}
        # Compound-parent completion: per-parent lock prevents race conditions
        # when multiple sub-tasks complete concurrently.
        self._compound_locks: dict[str, asyncio.Lock] = {}
        # Live status: short human-readable summary of what the agent is doing.
        self._live_status: dict[str, str] = {}

    @property
    def inflight(self) -> set[str]:
        return set(self._inflight)

    def get_live_status(self, task_id: str) -> str | None:
        """Return the latest live status summary for a task, or None."""
        return self._live_status.get(task_id)

    def _in_quota_cooldown(self, now: datetime) -> bool:
        return self._quota_cooldown_until is not None and now < self._quota_cooldown_until

    async def _claimable(self) -> list:
        out = []
        for status in _CLAIMABLE:
            for t in await self.store.list_tasks(status):
                if t.id not in self._inflight:
                    out.append(t)
        return out

    # Mid-run statuses only a live worker can hold. A task found in one of
    # these at STARTUP (this process's _inflight is empty by definition) was
    # orphaned by a crash/kill of the previous process.
    _ORPHANABLE = (TaskStatus.CONTEXT, TaskStatus.PLANNING,
                   TaskStatus.REVIEWING, TaskStatus.TESTING)

    async def _recover_orphans(self) -> None:
        """Startup-only crash recovery (review 2026-07-25): scoping the
        stuck-active sweep to claimed tasks fixed false parks but left a task
        orphaned mid-run (server killed during review/testing) INVISIBLE
        forever — not claimable, not swept, no escalation. At startup nothing
        is legitimately in-flight yet, so any mid-run status is an orphan:
        flip it to IMPLEMENTING (claimable) so the pool re-runs it from its
        checkpoint, and say so in the event stream."""
        for status in self._ORPHANABLE:
            for t in await self.store.list_tasks(status):
                await self.store.set_status(
                    t, TaskStatus.IMPLEMENTING, validate=False)
                await self.store.save_events(t.id, [{
                    "source": "orchestrator", "kind": "orphan_recovered",
                    "text": (f"found in {status.value} at startup with no "
                             "worker attached (previous process died mid-run) "
                             "— requeued from its checkpoint"),
                    "ts": time.time(),
                }])
                self._on_event(
                    "orphan_recovered",
                    f"{t.id[:8]} was orphaned in {status.value} — requeued")

    async def tick(self, *, now: datetime | None = None) -> list[str]:
        """One scheduling pass: resume parked tasks, then dispatch up to the free
        slots. Returns the task ids started this tick."""
        now = now or datetime.now(timezone.utc)
        if self.wake is not None:
            try:
                # Pass the claimed set so the stuck-active sweep judges only
                # tasks a worker is actually running — a resumed task waiting
                # for a free slot is silent by design, not hung.
                await self.wake.tick(now=now, active_ids=set(self._inflight))
            except Exception as exc:  # noqa: BLE001 — watcher must not kill the pool
                log.warning("wake tick failed: %s", exc)

        if self._in_quota_cooldown(now):
            return []  # 7.4: pool-wide pause until the subscription resets

        slots = self.max_workers - len(self._inflight)
        if slots <= 0:
            return []
        started: list[str] = []
        for task in (await self._claimable())[:slots]:
            self._inflight.add(task.id)          # reserve BEFORE scheduling
            asyncio.ensure_future(self._run(task))
            started.append(task.id)
        if started:
            self._on_event("dispatch", f"started {len(started)} task(s); "
                           f"{len(self._inflight)}/{self.max_workers} busy")
        # PR-E: periodic re-analysis (best-effort, never blocks task dispatch).
        if self.reanalysis is not None:
            try:
                ra_result = await self.reanalysis.maybe_run()
                if ra_result and ra_result.get("proposed", 0) > 0:
                    self._on_event(
                        "reanalysis",
                        f"proposed {ra_result['proposed']} learning(s) from "
                        f"{ra_result['transcripts']} transcript(s)",
                    )
            except Exception as exc:  # noqa: BLE001 — never kill the pool
                log.warning("re-analysis failed: %s", exc)
        # M-A: periodic repo-wiki refresh (best-effort, never blocks dispatch).
        if self.wiki_refresh is not None:
            try:
                wiki_result = await self.wiki_refresh.maybe_run()
                if wiki_result:
                    self._on_event(
                        "wiki_refresh",
                        f"refreshed wiki for {len(wiki_result)} repo(s)",
                    )
            except Exception as exc:  # noqa: BLE001 — never kill the pool
                log.warning("wiki refresh failed: %s", exc)
        return started

    def task_events(self, task_id: str) -> list[dict]:
        """Return captured events for a task (most recent last)."""
        return list(self._event_log.get(task_id, []))

    async def _run(self, task) -> None:
        # Set up per-task event capture.
        buf = deque(maxlen=self._MAX_EVENTS)
        self._event_log[task.id] = buf

        notify = self._event_notify.setdefault(task.id, asyncio.Event())

        # Events reach SQLite while the run is live, so a crash can't take the
        # whole history with it (and `buf` above is capped, so a chatty run used
        # to drop its earliest events even on a clean finish).
        persister = EventPersister(self.store, task.id,
                                   interval=self._EVENT_FLUSH_INTERVAL)

        def _sink(event):
            event["ts"] = time.time()
            # Don't clobber a subagent event's own task_id (the SDK's per-
            # dispatch Task-tool id, set in claude_backend.py's meta) — it's
            # a completely different concept from "which no_human task is
            # this," and overwriting it collapsed every distinct subagent
            # dispatch in a run down to one node in the System view.
            event.setdefault("task_id", task.id)
            buf.append(event)
            persister.record(event)
            notify.set()
            summary = _summarize_event(event)
            if summary:
                self._live_status[task.id] = summary

        persister.start()
        try:
            orch = self.factory(task)
            orch._sink = _sink
            outcome = await orch.run_task(task)
            # 7.4: a quota park pauses the whole pool until the reset time.
            if outcome is not None and outcome.status == TaskStatus.PAUSED_QUOTA:
                resets = _parse_iso(getattr(outcome.task, "wake_check_at", None))
                if resets is not None:
                    self._quota_cooldown_until = resets
                    self._on_event("quota_pause",
                                   f"pool paused until {resets.isoformat()}")
        except Exception as exc:  # noqa: BLE001 — one task must not kill the pool
            # logging only — a bare print/traceback to stderr raises
            # BrokenPipeError inside THIS except when the desktop parent that
            # piped our stderr has crashed away (SCRUM-11), killing the pool
            # worker the except exists to protect. logging.handleError swallows.
            log.warning("task %s crashed in pool: %s", task.id[:8], exc,
                        exc_info=True)
            self._on_event("task_error", f"{task.id[:8]}: {exc}")
            # Mark the task as FAILED so it doesn't stay stuck.
            try:
                from .task import TaskStatus as _TS
                await self.store.set_status(task, _TS.FAILED, validate=False)
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._inflight.discard(task.id)
            self._live_status.pop(task.id, None)
            # Final notify so SSE clients see the task finished, then clean up.
            if task.id in self._event_notify:
                self._event_notify[task.id].set()
                # Don't delete immediately — give SSE 5s to drain.
                # The SSE endpoint checks inflight and closes after idle ticks.

            # Stop the periodic flusher, then write whatever it hasn't taken.
            await persister.aclose()

            # Compound-parent completion: if this task is a sub-task, check
            # whether the parent compound task should transition.
            await self._check_compound_parent(task)

    async def _check_compound_parent(self, task) -> None:
        """After a sub-task finishes, check if the parent should complete."""
        parent_id = getattr(task, "parent_id", None)
        if not parent_id:
            return
        try:
            parent = await self.store.get_task(parent_id)
        except Exception:  # noqa: BLE001
            return
        if parent is None or parent.status != TaskStatus.COMPOUND_PARENT:
            return

        # Per-parent lock: prevents two concurrent sub-task completions from
        # racing on the same parent's check_completion.
        lock = self._compound_locks.setdefault(parent_id, asyncio.Lock())
        async with lock:
            try:
                from .lead_agent import LeadAgent
                lead = LeadAgent(
                    self.store,
                    config=self._config,
                    emit=lambda kind, text, **kw: self._on_event(kind, text),
                )
                finished = await lead.check_completion(parent)
                if finished:
                    self._compound_locks.pop(parent_id, None)
                    self._on_event(
                        "compound_resolved",
                        f"parent {parent_id[:8]} → {parent.status.value}",
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "compound completion check failed for %s: %s",
                    parent_id[:8], exc,
                )

    async def run_forever(
        self, *, stop: asyncio.Event, poll_interval: float = 10.0
    ) -> None:
        """Loop until ``stop`` is set, then drain in-flight tasks."""
        await self._recover_orphans()
        while not stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass
        await self.drain()

    async def drain(self) -> None:
        """Wait for all in-flight tasks to finish (best-effort, bounded poll)."""
        while self._inflight:
            await asyncio.sleep(0.05)


# --------------------------------------------------------------------------- #
# PR-E: Periodic re-analysis scheduler (EVOLUTION_PLAN Phase 9)                #
# --------------------------------------------------------------------------- #


class ReanalysisJob:
    """Periodically re-runs transcript extraction/ingestion on new sessions
    and proposes only patterns not already covered.

    All proposals land in ``learning/queue.py`` with ``confirmed=0`` — nothing
    activates without ``nh learnings --confirm``.

    Dedup is handled by the ingester's ``dedupe_key`` (content-based hash) and
    the transcript cache (Phase 7e): unchanged transcripts are skipped, and
    identical findings are never re-proposed.
    """

    def __init__(
        self,
        store: Store,
        *,
        interval_seconds: float = 86400,  # default: once per day
        days: int = 30,                    # look-back window for transcripts
        max_proposals_per_run: int = 20,   # cap to prevent queue flooding
        use_llm: bool = False,             # heuristic-only by default
        llm_call=None,                     # async (prompt) -> str, when use_llm=True
    ):
        self.store = store
        self.interval = max(60, interval_seconds)
        self.days = days
        self.max_proposals = max_proposals_per_run
        self.use_llm = use_llm
        self._llm_call = llm_call
        self._last_run: float = 0.0
        self._running = False

    def due(self, now: float | None = None) -> bool:
        """True when enough time has elapsed since the last run."""
        return (now or time.time()) - self._last_run >= self.interval

    async def maybe_run(self) -> dict | None:
        """Run re-analysis if due and not already running. Returns result dict
        or None if skipped. Thread-safe via ``_running`` flag."""
        if not self.due() or self._running:
            return None
        self._running = True
        try:
            return await self._run()
        finally:
            self._running = False
            self._last_run = time.time()

    async def _run(self) -> dict:
        from ..history.ingester import TranscriptIngester

        ingester = TranscriptIngester(self.store, llm_call=self._llm_call)
        result = await ingester.ingest(days=self.days, use_llm=self.use_llm)

        # Cap: if more proposals than max, keep only the first batch.
        # The surplus are already in the DB (ingester committed them), so
        # we log a warning but don't delete — the human can triage them.
        if result.proposed > self.max_proposals:
            log.warning(
                "re-analysis proposed %d items (cap %d) — review queue may be large",
                result.proposed, self.max_proposals,
            )

        log.info(
            "re-analysis complete: %d transcripts, %d findings, "
            "%d proposed, %d duplicates",
            result.transcripts, result.findings,
            result.proposed, result.duplicates,
        )
        return {
            "transcripts": result.transcripts,
            "findings": result.findings,
            "proposed": result.proposed,
            "duplicates": result.duplicates,
        }


# --------------------------------------------------------------------------- #
# Wiki refresh scheduler                                                       #
# --------------------------------------------------------------------------- #


class WikiRefreshJob:
    """Regenerates repo wiki docs when ``git rev-parse HEAD`` differs from the
    stored ``wiki_commit`` in the project profile.

    Same ``due()``/``maybe_run()`` shape as ``ReanalysisJob``; wired into the
    scheduler loop the same way.
    """

    def __init__(
        self,
        store: Store,
        backend: Any,
        *,
        interval_seconds: float = 3600,  # default: hourly check
        max_turns: int = 12,
    ):
        self.store = store
        self.backend = backend
        self.interval = max(60, interval_seconds)
        self.max_turns = max_turns
        self._last_run: float = 0.0
        self._running = False

    def due(self, now: float | None = None) -> bool:
        return (now or time.time()) - self._last_run >= self.interval

    async def maybe_run(self) -> list[dict] | None:
        """Check all profiled repos; regenerate wiki where HEAD moved."""
        if not self.due() or self._running:
            return None
        self._running = True
        try:
            return await self._run()
        finally:
            self._running = False
            self._last_run = time.time()

    async def _run(self) -> list[dict]:
        import subprocess
        from pathlib import Path

        from ..docs_gen import WikiGenerator
        from ..profile import ProjectProfile

        gen = WikiGenerator(self.backend, max_turns=self.max_turns)
        results: list[dict] = []

        projects = await self.store.list_projects()
        for proj in projects:
            for repo_path in (proj.get("repos") or []):
                repo = Path(repo_path).expanduser()
                if not repo.is_dir():
                    continue
                profile = ProjectProfile.load(repo)
                if not profile:
                    continue
                # Check HEAD vs stored wiki_commit.
                try:
                    r = subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        capture_output=True, text=True, timeout=5,
                        cwd=repo,
                    )
                    head = r.stdout.strip() if r.returncode == 0 else ""
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    continue
                if not head or head == profile.wiki_commit:
                    continue

                log.info("wiki refresh: %s HEAD %s (was %s)",
                         repo, head[:8], profile.wiki_commit[:8] or "none")
                result = await gen.generate(repo)
                if not result.error and result.commit_sha:
                    profile.wiki_commit = result.commit_sha
                    profile.save()
                results.append({
                    "repo": str(repo),
                    "files": result.files_written,
                    "error": result.error,
                })
        return results
