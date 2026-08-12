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
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Awaitable, Callable

from ..agent.worker_context import WorkerContext, set_worker_context
from ..config import parallelism_enabled, worktree_isolation_enabled
from .db import Store
from . import plan_gate
from .events import EventPersister
from .task import TaskStatus
from .worktree import sweep_stale_worktrees

log = logging.getLogger("no_human.scheduler")

# Tasks the scheduler may pick up: freshly created, or flipped back to
# IMPLEMENTING by the WakeWatcher / `nh reply` resume. IMPLEMENTING first —
# WIP-first: resumed work carries sunk cost and a waiting operator, and
# pending-first starved three budget-raised resumes behind every newly
# imported ticket on a one-worker pool (live, 2026-07-24).
_CLAIMABLE = (TaskStatus.IMPLEMENTING, TaskStatus.PENDING)


def pool_width_ceiling(cpu_count: int | None = None) -> int:
    """The widest pool this machine is allowed to run: ``cpu_count // 3``,
    never below 2.

    Why a third of the cores and not half: a worker is not one process. Each
    one drives a nested Agent-SDK subprocess (coder, then reviewer) *and* a
    test run, and ``bounded_xdist_workers`` below hands that test run
    ``cpu // max_workers`` pytest workers — so a pool's real CPU demand is
    several times its width. A third is a sanity margin against that
    arithmetic, not a measured optimum: the one measurement behind any of it
    is the incident ``bounded_xdist_workers`` was added for (3 tasks ×
    ``pytest -n auto`` on 12 cores timed every run out). Nothing here has
    been measured at cpu//2, so nothing here claims anything about it. The
    floor of 2 keeps the shipped default (2) reachable on a small machine
    rather than silently serialising it.
    """
    cpus = cpu_count if cpu_count is not None else (os.cpu_count() or 4)
    return max(2, int(cpus) // 3)


def clamp_pool_width(
    requested: int, *, cpu_count: int | None = None,
) -> tuple[int, str | None]:
    """``(width, reason)`` — the width to actually run, and why it is not the
    width that was asked for.

    ``nh start --workers 64`` used to be accepted in full: both resolvers
    bounded the pool from below (``max(1, ...)``) and not from above, so the
    only CPU-aware bound in the system was on the *children*
    (``bounded_xdist_workers``, added after 3 tasks × ``pytest -n auto`` on 12
    cores timed every run out) and nothing at all bounded the *parents*. The
    ceiling is loud rather than silent for the same reason the isolation
    refusal below is: an operator who asked for a width and got a different one
    must be told which one is running, and why.

    ``cpu_count`` is injectable so the boundary is testable on any machine.
    """
    ceiling = pool_width_ceiling(cpu_count)
    if requested <= ceiling:
        return requested, None
    cpus = cpu_count if cpu_count is not None else (os.cpu_count() or 4)
    return ceiling, (
        f"{requested} workers is above this machine's ceiling of {ceiling} "
        f"({cpus} cores // 3, floor 2) - running {ceiling}, not {requested}. "
        "This is a sanity ceiling against absurd widths, not a tuned optimum: "
        "every worker drives its own nested agent subprocesses AND its own "
        "pytest -n run, whose width is already cpu//workers, so a third of the "
        "cores is where the test phase starts starving itself. It is not a "
        "stability guarantee - nested-subprocess instability at even small "
        "widths is a known separate issue, tracked and fixed on its own "
        f"branch. Ask for {ceiling} or fewer, or raise the ceiling only after "
        "re-measuring on this machine."
    )


def resolve_max_workers(
    config: dict, *, override: int | None = None, cpu_count: int | None = None,
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

    The width is bounded from ABOVE too (``clamp_pool_width``): the isolation
    and concurrency switches decide whether a pool is allowed at all, and the
    ceiling decides how wide this machine can carry it. The two are checked in
    that order, so a downgrade to 1 still reports the switch that caused it.
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
    return clamp_pool_width(requested, cpu_count=cpu_count)


def resolve_serve_pool(
    config: dict, *, cli_workers: int | None, cpu_count: int | None = None,
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

    Both widths are then bounded from above by ``clamp_pool_width``. The
    clamp is a downgrade, not a refusal, so it does NOT travel in ``error``
    (which means "do not serve"): ``serve()`` re-derives the one-line reason
    from the width that was asked for and prints it, the same way ``nh start``
    prints ``resolve_max_workers``'s warning.
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
        return clamp_pool_width(cli_workers, cpu_count=cpu_count)[0], True, None

    if not parallelism_enabled(config):
        return 0, False, (
            "concurrency.enabled is false - set it in ~/.no_human/config.yaml "
            "to run the pool, or pass --max-workers N to enable it for this "
            "run. Refusing to serve a pool that was not asked for."
        )
    workers = int(conc.get("max_workers", 2) or 2)
    if workers > 1 and not isolated:
        # The refusal quotes the width the operator configured, not the
        # clamped one: they need to recognise the number they wrote.
        return 0, False, _no_isolation(workers)
    return clamp_pool_width(workers, cpu_count=cpu_count)[0], True, None


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
        # Every id this process has dispatched, kept after it finishes: the
        # drain's exit code is about what THIS run did, not about whatever
        # else is in the database.
        self._dispatched: set[str] = set()
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
        # --- liveness (2026-08-01 incident) ------------------------------- #
        # `inflight: 0` read identically for "queue empty" and "the connection
        # is serving a three-hour-old snapshot and I am crashing 12x/minute".
        # These make the two distinguishable from outside the process.
        self._last_tick_at: float | None = None
        self._last_dispatch_at: float | None = None
        self._last_claimable_count: int | None = None
        self._crash_times: deque = deque(maxlen=500)
        self._db_view_stale = False
        self._db_stale_since: float | None = None
        self._status_write_failures = 0
        self._consecutive_status_write_failures = 0
        self._last_status_write_error: str | None = None
        # The probe can fail too, and a detector that fails open is worth
        # nothing: without these, "the probe raised on every tick for three
        # hours" and "the view is fine" are the same JSON. See
        # `_check_db_liveness`.
        self._probe_failures = 0
        self._consecutive_probe_failures = 0
        self._last_probe_error: str | None = None
        # `run_forever` overwrites this with the interval it was actually given.
        self._poll_interval = 10.0
        # When this Scheduler came into existence. The anchor for "has never
        # ticked, and has had long enough that it should have" — without it,
        # "never ticked" is indistinguishable from "started four seconds ago".
        self._created_at = time.time()

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
        # A plan-approval correction resumes into PLANNING, not IMPLEMENTING —
        # it must be re-planned before a token is spent implementing it. That
        # status is otherwise mid-run-only, so it is claimed here on the one
        # marker a live planning worker can never carry: a correction a human
        # left on a parked task (`plan_gate.correcting` — keyed on the STATE,
        # never on the correction text: a blank answer used to write the state
        # with empty text, which this claim missed and the orphan sweep below
        # then turned into a gate bypass).
        for t in await self.store.list_tasks(TaskStatus.PLANNING):
            if t.id not in self._inflight and plan_gate.correcting(t):
                out.append(t)
        return out

    # Mid-run statuses only a live worker can hold. A task found in one of
    # these at STARTUP (this process's _inflight is empty by definition) was
    # orphaned by a crash/kill of the previous process.
    _ORPHANABLE = (TaskStatus.CONTEXT, TaskStatus.PLANNING,
                   TaskStatus.REVIEWING, TaskStatus.TESTING)

    # Runtime sweep only: a mid-run row whose last persisted ACTIVITY (row
    # updated_at OR newest task event — a live run keeps flushing events from
    # whatever process drives it, even one this scheduler knows nothing
    # about) is younger than this is left alone. Generous on purpose: a
    # genuinely stranded row otherwise waits forever, so fifteen minutes
    # costs little, while a live out-of-process run's longest silent stretch
    # (a full-suite pytest inside review) must fit under it.
    _STRANDED_GRACE_S = 900.0

    async def _reconcile_terminal_task_attempts(self) -> None:
        """Startup-only: retire attempt rows left open on tasks that finished.

        Separate from `_recover_orphans` on purpose. That sweep iterates
        `_ORPHANABLE`, i.e. NON-terminal task statuses, so a `done`/`failed`
        task holding an `in_progress` row is invisible to it — which is why 42
        such rows had accumulated by 2026-08-11, the oldest open for 32 days,
        and why one task stayed hidden behind one for nine days. See
        `Store.close_attempts_of_terminal_tasks` for why this is startup-only.
        """
        try:
            n = await self.store.close_attempts_of_terminal_tasks()
        except Exception:  # noqa: BLE001 — reconciliation must never block boot
            log.exception("startup: reconciling terminal-task attempts failed")
            return
        if n:
            # Say it out loud. A silent reconciliation of 42 rows is
            # indistinguishable from a bug that closed rows it should not have.
            log.info("startup: retired %d attempt row(s) left open on tasks "
                     "that had already finished", n)

    async def _sweep_stale_worktrees(self) -> None:
        """Startup-only: reclaim worktree directories left by tasks that
        already reached a terminal status and will never run again.

        Mirrors `_reconcile_terminal_task_attempts` above — same
        "must never block boot" wrapping, same reason it runs once at startup
        and not on a per-tick cadence. See `core/worktree.sweep_stale_worktrees`
        for the reclaim rules (never age/mtime — only a provably dead owner or
        a provably vanished git admin dir)."""
        try:
            removed, skipped = await sweep_stale_worktrees(self.store, self._config)
        except Exception:  # noqa: BLE001 — the sweep must never block boot
            log.exception("startup: sweeping stale worktrees failed")
            return
        if removed or skipped:
            log.info(
                "startup: reclaimed %d stale worktree(s), skipped %d",
                removed, skipped)

    async def _recover_orphans(self, *, startup: bool = True) -> None:
        """Crash/strand recovery. At STARTUP (review 2026-07-25): nothing is
        legitimately in-flight yet, so any mid-run status is an orphan of a
        killed process — flip it to IMPLEMENTING (claimable) so the pool
        re-runs it from its checkpoint. Since R15 (2026-08-09 incident) the
        sweep also runs PER-TICK with ``startup=False``: a status write that
        is lost or reverted (a stale full-row write-back did exactly that)
        strands a row in a worker-only status while the process lives, and
        the startup-only sweep left it invisible until the next restart —
        66 minutes, in the incident. At runtime a row is an orphan only when
        no live worker has it claimed AND it has not been touched for
        ``_STRANDED_GRACE_S``."""
        for status in self._ORPHANABLE:
            for t in await self.store.list_tasks(status):
                # Not an orphan: a task sitting in PLANNING with a human's
                # plan correction on it is WAITING to be re-planned, and
                # requeueing it as IMPLEMENTING would spend the run on the
                # very plan they rejected. `_claimable` picks it up instead.
                if plan_gate.correcting(t):
                    continue
                if not startup:
                    if t.id in self._inflight:
                        continue
                    # Liveness is judged on the newest persisted activity —
                    # row write OR event — because `_inflight` is per-process:
                    # a CLI-driven run in another process is invisible to it,
                    # but its events land in the same DB (B1, review
                    # 2026-08-10; every in-process runner, including the
                    # `nh watch` TUI, persists through EventPersister).
                    if self._row_age_s(t.updated_at) < self._STRANDED_GRACE_S:
                        continue      # row itself is young — no query needed
                    ev_ts = await self.store.last_event_ts(t.id)
                    if (ev_ts is not None
                            and time.time() - ev_ts < self._STRANDED_GRACE_S):
                        continue
                    text = (f"found in {status.value} with no worker attached "
                            "(status write lost, or its worker died) — "
                            "requeued from its checkpoint")
                else:
                    text = (f"found in {status.value} at startup with no "
                            "worker attached (previous process died mid-run) "
                            "— requeued from its checkpoint")
                # THE STATUS WRITE GOES FIRST, and nothing else happens until it
                # lands. `set_status` CAS-guards terminal rows (SCRUM-73) and
                # returns None when it refuses — a human can mark this task DONE
                # or cancel it between the `list_tasks` read above and here, and
                # `merge_context` has no rollback, so stamping first left a
                # FINISHED task holding an `orphan_recovery` checkpoint nothing
                # would ever clear. A lost race here used to be free because the
                # sweep wrote nothing; it is not free any more.
                if await self.store.set_status(
                        t, TaskStatus.IMPLEMENTING, validate=False) is None:
                    continue
                try:
                    sha = await self._inherited_checkpoint(t)
                except Exception:  # noqa: BLE001 — sweep must not kill the pool
                    # One task's checkpoint must never abort the sweep. This is
                    # the code path whose whole job is to rescue tasks after a
                    # crash, and an unguarded raise inside the loop took every
                    # REMAINING orphan down with it. A checkpoint is an
                    # optimisation; requeueing is the rescue — which has already
                    # happened above, so a failure here costs a cold start.
                    log.exception(
                        "orphan recovery: checkpoint lookup failed for %s — "
                        "requeued from base", t.id[:8])
                    sha = ""
                if sha:
                    text += f" {sha[:8]}"
                await self.store.save_events(t.id, [{
                    "source": "orchestrator", "kind": "orphan_recovered",
                    "text": text,
                    "ts": time.time(),
                }])
                self._on_event(
                    "orphan_recovered",
                    f"{t.id[:8]} was orphaned in {status.value} — requeued")

    async def _inherited_checkpoint(self, t) -> str:
        """The sha the requeued run must branch from, stamped if it is not
        already recorded. Returns it, or "" when there is nothing to resume.

        🔴 A REQUEUE IS NOT A RESTART, AND THE ROW ALONE DOES NOT SAY SO.
        `run_task` re-enters as a FRESH bounded loop at ``attempt_n == 1``,
        where `_resume_branch_point` ignores ``handoff.wip_sha`` (attempt 1 has
        no predecessor of its own) — so the only checkpoint that survives a
        requeue is ``resume_from``, which is not attempt-gated. The dead
        attempt's work is committed and its sha is on the attempt row, but
        nothing copied it there: measured 2026-08-10, three restarts closed 11
        attempts as 'superseded by a newer attempt' across 5 live tasks and
        every successor re-ran the coder from base. No loss was reported
        (`resume_checkpoint_lost` NULL on all of them) because no checkpoint
        was ever considered.

        Provenance is the machine's, never ``human``: the zero-diff honesty
        gate (`Orchestrator._is_own_partial`) credits work already ahead of
        base only when a human gated the branch point, and a requeue is a
        machine decision. A HUMAN's ``resume_from`` is INHERITED untouched —
        stamping over their gated sha relabels it as the machine's and fails
        their resume as fabrication, which is the bug that gate keeps
        re-learning; the human is identified by the SAME rule that gate uses,
        including its legacy fallback, so the two cannot drift apart. A sha the
        object store can no longer read needs no check here: the orchestrator
        already falls back to base and says so (`resume_checkpoint_lost`).

        🔴 TWO THINGS THIS MUST NOT DO, both found by review of the first cut.

        It must not inherit its OWN earlier stamp. The measured incident was
        THREE restarts. Bailing on any existing ``resume_from`` cannot tell a
        human's gate from the machine's last one, so restart 2 resumed onto
        restart 1's sha and discarded everything the run in between committed —
        the same loss, one restart later. Only ``human`` is inherited.

        And the candidate is the attempt that actually DIED —
        `Store.latest_open_attempt`, the most recently STARTED row still
        ``in_progress``, and only that row. "The newest attempt this task ever
        had" reaches back PAST a deliberate clear: `POST /tasks/{id}/retry`,
        `nh task retry`, `LeadAgent`'s sub-task retry, `_unblock_ready` and
        both "send back" twins all drop the checkpoint because a fresh run must
        not branch from one an earlier actor chose, and the first sweep after
        any of them put it straight back, from a run that had already failed.
        Those paths now call `Store.close_open_attempts` FIRST, so after a
        clear there is no open row to reach back to; the reason that fix is
        there and not here is written out on that method — the context a clear
        leaves behind is indistinguishable from a task that never had a
        checkpoint, so no amount of reading it can tell them apart.

        Not a search for "the newest open row that HAS a sha", either. Falling
        past an open row with no commit is the same reach-back by another
        route: after a clear the requeued run's own row is open and empty, and
        skipping it lands on the pre-clear row. If the attempt that died
        committed nothing, there is nothing to resume, and a cold start is the
        correct answer.
        """
        ctx = t.context or {}
        resume = ctx.get("resume_from") or {}
        by = resume.get("by")
        if resume.get("sha") and (
                by == "human"
                # No stamp at all is a row written before provenance existed;
                # `wake.py` has always set `resume_reason`, so anything else is
                # a legacy HUMAN resume. Identical to `_is_own_partial`'s tail.
                or (not by
                    and ctx.get("resume_reason") != "wake_condition_satisfied")):
            return ""                    # a human gated it — execute, don't decide
        sha = (await self.store.latest_open_attempt(t.id) or {}).get("commit_sha") or ""
        if not sha:
            # Nothing newer to point at. Deliberately NOT a clear: an earlier
            # machine stamp is the only thing still protecting that work.
            return ""
        if sha == resume.get("sha"):
            return sha                   # already stamped — no write, no churn
        from ..blockers import resume_provenance
        t.context = await self.store.merge_context(
            t.id, {"resume_from": resume_provenance(
                {"sha": sha}, "orphan_recovery")})
        return sha

    @staticmethod
    def _row_age_s(stamp: str | None) -> float:
        """Seconds since an isoformat stamp; unparseable reads as 0 (young),
        so a corrupt timestamp can never cause a false requeue."""
        try:
            return max(0.0, time.time()
                       - datetime.fromisoformat(str(stamp)).timestamp())
        except (ValueError, TypeError):
            return 0.0

    async def _check_db_liveness(self) -> None:
        """Per-tick: is the Store's read view still the database's?

        THIS IS THE GUARD THAT SURVIVES AN UNKNOWN FIRST CAUSE. The rollback
        added in `db.py` closes one route to a pinned connection, but the
        2026-08-01 incident's timeline does not fit that route — the process was
        pinned three hours before its own first read, i.e. stale from birth,
        which points at a recovered stale WAL index rather than at anything this
        process did. Detection does not need to know: whatever pins the
        connection, the pinned connection disagrees with the file, and that
        disagreement is what is measured here.

        Never raises. A probe that can kill the pool is worse than no probe —
        and the probe touches a second connection, so it has its own failure
        modes (a full disk, a locked file) that must not become the pool's.
        """
        probe = getattr(self.store, "probe_snapshot_staleness", None)
        if probe is None:      # a Store double in a test; nothing to check
            return
        try:
            result = await probe()
        except Exception as exc:  # noqa: BLE001 — never kill the pool
            # FAILING OPEN SILENTLY IS THE INCIDENT'S OWN SHAPE. Returning here
            # without a counter left `db_view_stale: False`, `healthy: true`,
            # `idle_reason: queue_empty` — byte-identical to health — while the
            # only thing that knew better was a log line, back in the 46,000
            # lines this change exists to make unnecessary. A detector that
            # cannot report its own failure is not a detector.
            #
            # It still must not kill the pool, so the tick continues; what
            # changes is that the failure is now COUNTED and published, exactly
            # like `status_write_failures`.
            self._probe_failures += 1
            self._consecutive_probe_failures += 1
            self._last_probe_error = f"{type(exc).__name__}: {exc}"
            log.error(
                "db staleness probe FAILED (%d in a row): %s. While it is "
                "failing, nothing is checking whether the scheduler's view of "
                "the queue is real — treat the view as UNKNOWN, not healthy.",
                self._consecutive_probe_failures, exc, exc_info=True)
            self._on_event(
                "db_probe_failed",
                f"staleness probe failed ({self._consecutive_probe_failures} "
                f"in a row): {exc}")
            return
        self._consecutive_probe_failures = 0
        if not result.stale:
            if self._db_view_stale:
                log.warning("db read view recovered")
                self._on_event("db_view_recovered",
                               "database read view is live again")
            self._db_view_stale = False
            self._db_stale_since = None
            return

        if not self._db_view_stale:
            self._db_stale_since = time.time()
        self._db_view_stale = True
        # ERROR, not warning: on 2026-08-01 this state ran for six hours with
        # every surface reporting health. It is never routine.
        log.error(
            "DATABASE READ VIEW IS FROZEN: this connection sees %d task(s) up "
            "to %s, the file has %d up to %s. Every task written since is "
            "INVISIBLE to the scheduler and cannot be dispatched. Reconnecting.",
            result.shared.count, result.shared.max_updated_at,
            result.fresh.count, result.fresh.max_updated_at)
        self._on_event(
            "db_view_stale",
            f"frozen read snapshot: connection sees {result.shared.count} "
            f"task(s), file has {result.fresh.count} — reconnecting")

        reconnect = getattr(self.store, "reconnect", None)
        if reconnect is None:
            return
        try:
            await reconnect()
        except Exception as exc:  # noqa: BLE001
            log.error("reconnect after a frozen read view FAILED: %s", exc,
                      exc_info=True)
            self._on_event("db_reconnect_failed", str(exc))
            return
        # Verify the recovery rather than assume it. If the view is still
        # frozen after a fresh connection, the cause is not this connection and
        # the flag must stay up — silently clearing it would recreate exactly
        # the false all-clear this whole change exists to remove.
        try:
            after = await probe()
        except Exception as exc:  # noqa: BLE001
            log.warning("post-reconnect staleness probe failed: %s", exc)
            return
        if after.stale:
            log.error("STILL FROZEN after reconnecting — the stale view is not "
                      "this connection's doing; escalate")
            self._on_event("db_view_stale",
                           "still frozen after reconnect — needs a human")
            return
        self._db_view_stale = False
        self._db_stale_since = None
        log.warning("reconnected; read view is live again (now sees %d task(s))",
                    after.shared.count)
        self._on_event("db_view_recovered",
                       f"reconnected — now sees {after.shared.count} task(s)")

    def _crashes_since(self, seconds: float) -> int:
        cutoff = time.time() - seconds
        return sum(1 for t in self._crash_times if t >= cutoff)

    def health_snapshot(self) -> dict:
        """What `/api/worker/status` needs to tell idle from wedged.

        `inflight: 0` is the same number in both states — it was honest
        throughout the 2026-08-01 incident, because each crash was instantaneous
        and a poll almost never landed inside a run. So the number is kept and
        the CONTEXT is added: `idle_reason` names why nothing is running, and
        the counters say whether the last tick achieved anything.
        """
        inflight = len(self._inflight)
        since_tick = (None if self._last_tick_at is None
                      else time.time() - self._last_tick_at)
        # A LOOP THAT HAS STOPPED TICKING INVALIDATES EVERY OTHER FIELD HERE,
        # which is why it is judged first. `db_view_stale`, `claimable` and the
        # crash counts are all written BY a tick, so a stalled loop freezes them
        # at their last values and they read healthy forever — the same failure this
        # change exists to remove, one level up. Publishing
        # `seconds_since_last_tick` and leaving it out of `healthy` was not
        # enough: a field nobody reads surfaces nothing.
        #
        # The threshold is derived from the interval the loop was actually
        # given, not a constant, so it stays correct under any `poll_interval`.
        # Six intervals (>=60s) is deliberately generous: a tick does real work
        # — the wake watcher, the probe, re-analysis — and a false "stalled"
        # would be its own bad alarm. A tick that genuinely takes over a minute
        # is also not dispatching, so reporting it is right either way.
        stall_after = max(60.0, self._poll_interval * 6)
        # A LOOP THAT HAS NEVER TICKED AT ALL is the same failure one step
        # earlier, and the first version of this reported it healthy FOREVER:
        # `since_tick` is None before the first tick, so `since_tick > ...` was
        # never evaluated and `tick_stalled` stayed False. The `starting` branch
        # below existed and simply was not carried into `healthy`.
        #
        # It is REACHABLE, and by this incident's own mechanism: `run_forever`
        # awaits `_recover_orphans()` before the loop with no try/except, and
        # that method issues unguarded `set_status`/`save_events` WRITES. A
        # connection wedged at startup — "stale from birth", the inferred first
        # cause of the incident this module now guards against — fails those
        # writes with `database is locked`, killing `run_forever` before tick 1.
        #
        # BOUNDED, not unconditional: `healthy: true` during the genuine first
        # poll_interval is correct, and a server that has been up for four
        # seconds must not report a fault. So the same threshold applies, timed
        # from when this Scheduler was constructed.
        never_ticked_too_long = (
            self._last_tick_at is None
            and time.time() - self._created_at > stall_after)
        tick_stalled = (
            (since_tick is not None and since_tick > stall_after)
            or never_ticked_too_long)

        # ORDERING. Faults first, then ordinary operating states. A fault
        # reported as `slots_full` or `queue_empty` is the defect this whole
        # change exists to remove, so nothing that describes NORMAL operation
        # may mask something that describes a BREAKAGE. Every adjacent pair
        # below is pinned by a test that constructs both conditions at once —
        # the argument used to live only in this comment, where it was free to
        # regress silently.
        if never_ticked_too_long:
            # Distinguished from `tick_loop_stalled`: "it never started" and "it
            # stopped" have different causes and different first questions.
            idle_reason = "never_ticked"
        elif tick_stalled:
            # Outranks everything: `db_view_stale`, `claimable` and the crash
            # counts are all written BY a tick, so a stalled loop freezes them
            # at their last values and they all read healthy. Their content is
            # not evidence any more, and saying so first is the honest report.
            idle_reason = "tick_loop_stalled"
        elif self._db_view_stale:
            # A CONFIRMED observation, so it outranks the probe-failure case
            # below (an ABSENCE of information) and every normal state. The
            # queue looks empty precisely BECAUSE the view is frozen, so
            # reporting `queue_empty` here would be the original lie in a new
            # field.
            idle_reason = "db_view_stale"
        elif self._consecutive_probe_failures:
            # Not "the view is stale" — "nobody knows whether it is".
            idle_reason = "db_probe_failing"
        elif self._last_tick_at is None:
            # Genuinely still starting, inside the first threshold.
            idle_reason = "starting"
        elif inflight >= self.max_workers:
            idle_reason = "slots_full"
        elif self._quota_cooldown_until is not None and \
                datetime.now(timezone.utc) < self._quota_cooldown_until:
            idle_reason = "quota_cooldown"
        elif inflight > 0:
            idle_reason = None
        elif self._last_claimable_count:
            idle_reason = "claimable_not_dispatched"
        else:
            idle_reason = "queue_empty"

        store_liveness = {}
        getter = getattr(self.store, "liveness", None)
        if callable(getter):
            try:
                store_liveness = getter()
            except Exception:  # noqa: BLE001
                store_liveness = {}
        return {
            "idle_reason": idle_reason,
            "db_view_stale": self._db_view_stale,
            "db_stale_since": self._db_stale_since,
            "last_tick_at": self._last_tick_at,
            # HOW FRESH `db_view_stale` IS. The probe runs per TICK, not per
            # request: a status endpoint that opened a second SQLite connection
            # on every poll would put the board's polling on the database's
            # critical path. So this flag is at most one poll_interval old, and
            # a caller that needs to know that is told rather than left to
            # assume. It is also the alarm for the other silent failure — a
            # scheduler whose loop has stopped ticking at all, which no
            # per-tick check can ever report on its own.
            "seconds_since_last_tick": (
                None if since_tick is None else round(since_tick, 1)),
            "tick_stalled": tick_stalled,
            "never_ticked": never_ticked_too_long,
            "seconds_since_start": round(time.time() - self._created_at, 1),
            "tick_stall_threshold_s": stall_after,
            "probe_failures": self._probe_failures,
            "consecutive_probe_failures": self._consecutive_probe_failures,
            "last_probe_error": self._last_probe_error,
            "last_dispatch_at": self._last_dispatch_at,
            "claimable": self._last_claimable_count,
            "crashes_last_5m": self._crashes_since(300),
            "crashes_last_1m": self._crashes_since(60),
            "status_write_failures": self._status_write_failures,
            "consecutive_status_write_failures":
                self._consecutive_status_write_failures,
            "last_status_write_error": self._last_status_write_error,
            "quota_cooldown_until": (self._quota_cooldown_until.isoformat()
                                     if self._quota_cooldown_until else None),
            "db": store_liveness,
        }

    async def tick(self, *, now: datetime | None = None) -> list[str]:
        """One scheduling pass: resume parked tasks, then dispatch up to the free
        slots. Returns the task ids started this tick."""
        now = now or datetime.now(timezone.utc)
        self._last_tick_at = time.time()
        # BEFORE anything reads the queue. Every decision below this line is
        # made from `store`, so if the connection's view is frozen the whole
        # tick is reasoning about a database that no longer exists.
        await self._check_db_liveness()
        if self.wake is not None:
            try:
                # Pass the claimed set so the stuck-active sweep judges only
                # tasks a worker is actually running — a resumed task waiting
                # for a free slot is silent by design, not hung.
                await self.wake.tick(now=now, active_ids=set(self._inflight))
            except Exception as exc:  # noqa: BLE001 — watcher must not kill the pool
                log.warning("wake tick failed: %s", exc)

        # R15: a lost/reverted status write strands a task in a worker-only
        # status with no worker attached — sweep every tick, not only at
        # startup, or it stays invisible until the next restart.
        try:
            await self._recover_orphans(startup=False)
        except Exception as exc:  # noqa: BLE001 — sweep must not kill the pool
            log.warning("stranded-task sweep failed: %s", exc)

        if self._in_quota_cooldown(now):
            return []  # 7.4: pool-wide pause until the subscription resets

        slots = self.max_workers - len(self._inflight)
        if slots <= 0:
            return []
        started: list[str] = []
        claimable = await self._claimable()
        self._last_claimable_count = len(claimable)
        for task in claimable[:slots]:
            self._inflight.add(task.id)          # reserve BEFORE scheduling
            asyncio.ensure_future(self._run(task))
            started.append(task.id)
        if started:
            self._dispatched.update(started)
            self._last_dispatch_at = time.time()
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
        # Bind this worker's identity + the concurrency it was dispatched into,
        # BEFORE anything can fail. A nested Agent SDK session that dies in the
        # transport reads this to say which worker died and how many peers were
        # live (see agent/worker_context.py); with nothing bound it can only
        # say "unknown", which is how the 2026-07-11 "Stream closed" incident
        # ended up un-attributable.
        #
        # Set INSIDE the coroutine, not at the `ensure_future` call site:
        # `ensure_future` copies the current context into the new task, so a
        # set() in here is private to this task and cannot be clobbered by the
        # next worker the same tick dispatches.
        set_worker_context(WorkerContext(
            worker=task.id[:8],
            # `_inflight` already contains this task — `tick()` reserves the id
            # synchronously before scheduling — so a lone worker reads 1 of 1.
            inflight=len(self._inflight),
            max_workers=self.max_workers,
        ))
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
            self._crash_times.append(time.time())
            # Durable reason. `_on_event` above is the LIVE pool stream — it is
            # gone the moment nobody is watching, and `log.warning` lands in a
            # file the board never reads. Without this the task is FAILED with
            # no recorded cause anywhere a human looks: the drawer's "Why it
            # failed" reads the last attempt's failure_reason, and a crash here
            # can happen with no attempt row to carry one. Same durable channel
            # this file already uses for `orphan_recovered` above.
            #
            # Its own try: a write that fails must not cost us the set_status
            # below, and neither may kill the pool worker this except exists to
            # protect (see the stderr note above — that hazard is real, but it
            # is about writing to a broken pipe, not about the store).
            try:
                await self.store.save_events(task.id, [{
                    "source": "scheduler", "kind": "task_crashed",
                    "text": f"{type(exc).__name__}: {exc}",
                    "ts": time.time(),
                }])
            except Exception:  # noqa: BLE001
                pass
            # Mark the task as FAILED so it doesn't stay stuck.
            try:
                from .task import TaskStatus as _TS
                await self.store.set_status(task, _TS.FAILED, validate=False)
                self._consecutive_status_write_failures = 0
            except Exception as werr:  # noqa: BLE001
                # This used to be `except Exception: pass`, with no counter.
                # That is the line that made the 2026-08-01 wedge silent: when
                # the DB is the broken thing, THIS write fails too, the task
                # keeps a claimable status, and the next tick re-dispatches it —
                # ~12x/minute for three hours, with nothing logged above debug.
                #
                # It stays non-fatal (one task must not kill the pool), but a
                # crash handler whose own fallback cannot write is a DB-LEVEL
                # alarm, not a per-task detail, and is reported as one. No
                # retry cap is added here on purpose: during the incident the
                # scheduler was behaving correctly on the data it could see, so
                # capping retries would have hidden the fault instead of fixing
                # it. Visibility is the fix; `_check_db_liveness` is the cure.
                self._status_write_failures += 1
                self._consecutive_status_write_failures += 1
                self._last_status_write_error = f"{type(werr).__name__}: {werr}"
                log.error(
                    "could not mark task %s FAILED after its crash: %s. The "
                    "task keeps a claimable status and WILL be re-dispatched. "
                    "%d consecutive status-write failure(s) — if this is "
                    "climbing, the database connection is the fault, not the "
                    "task.", task.id[:8], werr,
                    self._consecutive_status_write_failures, exc_info=True)
                self._on_event(
                    "status_write_failed",
                    f"{task.id[:8]}: could not record FAILED ({werr}); "
                    f"{self._consecutive_status_write_failures} in a row")
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
        self, *, stop: asyncio.Event, poll_interval: float = 10.0,
        until_empty: bool = False,
    ) -> None:
        """Loop until ``stop`` is set, then drain in-flight tasks.

        ``until_empty`` (``nh serve --until-empty``) sets that SAME ``stop``
        event as soon as a tick leaves the queue drained. It is deliberately
        one more way to raise the flag ctrl-c/SIGTERM already raise, not a
        second shutdown path: everything after the loop is unchanged. Parked
        work (blocked / awaiting-input / escalated / paused-quota) is not
        claimable, so it ENDS the drain rather than holding the process open
        for a human who is not there.
        """
        # Recorded so `health_snapshot` can judge "this loop has stopped
        # ticking" against the interval it is SUPPOSED to tick at, rather than
        # against a constant that would be wrong for any other configuration.
        self._poll_interval = float(poll_interval)
        # BEFORE the orphan sweep: that sweep reads `latest_open_attempt` to
        # recover a checkpoint, and a row left open on a task that already
        # FINISHED is not a checkpoint to resume from — it is debris.
        await self._reconcile_terminal_task_attempts()
        await self._sweep_stale_worktrees()
        await self._recover_orphans()
        while not stop.is_set():
            await self.tick()
            # After the tick, so a task dispatched this pass is already in
            # `_inflight` and an empty queue can never be read mid-dispatch.
            if until_empty and await self.queue_is_drained():
                stop.set()
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass
        await self.drain()

    async def queue_is_drained(self) -> bool:
        """Nothing running, nothing left to claim — the drain's exit condition.

        Also the CLI's after-the-fact check: a run that stopped on a SIGNAL
        with work still queued did NOT drain, and must not report that it did.
        """
        return not self._inflight and not await self._claimable()

    async def failed_dispatched(self) -> list[str]:
        """Ids this process dispatched whose task ended ``TaskStatus.FAILED``.

        `task.status` is the field, and FAILED is the only value on it that
        means failure: `core/task.py`'s TERMINAL_STATES is {DONE, FAILED}, and
        every other resting place — awaiting_approval, blocked, awaiting_input,
        escalated, paused_quota — is an honest park the loop is designed to
        produce. A pool-level crash lands here too: `_run`'s handler writes
        FAILED (and, if even that write fails, it says so loudly rather than
        silently downgrading the outcome).
        """
        out: list[str] = []
        for tid in sorted(self._dispatched):
            task = await self.store.get_task(tid)
            if task is not None and task.status == TaskStatus.FAILED:
                out.append(tid)
        return out

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
