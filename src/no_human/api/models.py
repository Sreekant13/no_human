"""Pydantic response models for the no_human board API."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, model_validator

from ..core.db import USAGE_ROLES, usage_columns_for
from ..core.task import Task


class AttemptOut(BaseModel):
    id: str
    attempt_number: int
    branch_name: str | None = None
    commit_sha: str | None = None
    pr_url: str | None = None
    review_passed: int | None = None
    review_checklist: dict | None = None
    test_results: dict | None = None
    ci_pipeline_id: str | None = None
    ci_pipeline_url: str | None = None
    ci_status: str | None = None
    failure_reason: str | None = None
    # The checkpoint this attempt could not resume from (see db.py). Explicitly
    # part of the wire shape: the drawer is where a human asks why an attempt
    # started from scratch, and a field omitted here reads as "never happened".
    resume_checkpoint_lost: str | None = None
    # …and the one it DID branch from. Its sibling above records only failures,
    # so a surface carrying that alone answers "why did this start from
    # scratch?" and can never answer "did resuming work?" — which is the
    # question the crash-requeue path exists to move, and it had no reader
    # outside SQL.
    resume_checkpoint: str | None = None
    turns_used: int | None = None
    tokens_used: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    status: str | None = None
    started_at: str | None = None
    completed_at: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "AttemptOut":
        def _json(v: Any) -> Any:
            if isinstance(v, str):
                try:
                    return json.loads(v)
                except (json.JSONDecodeError, ValueError):
                    pass
            return v

        return cls(
            id=row.get("id", ""),
            attempt_number=row.get("attempt_number", 0),
            branch_name=row.get("branch_name"),
            commit_sha=row.get("commit_sha"),
            pr_url=row.get("pr_url"),
            review_passed=row.get("review_passed"),
            review_checklist=_json(row.get("review_checklist")),
            test_results=_json(row.get("test_results")),
            ci_pipeline_id=row.get("ci_pipeline_id"),
            ci_pipeline_url=row.get("ci_pipeline_url"),
            ci_status=row.get("ci_status"),
            failure_reason=row.get("failure_reason"),
            resume_checkpoint_lost=row.get("resume_checkpoint_lost"),
            resume_checkpoint=row.get("resume_checkpoint"),
            turns_used=row.get("turns_used"),
            tokens_used=row.get("tokens_used"),
            cache_read_tokens=row.get("cache_read_tokens"),
            cache_creation_tokens=row.get("cache_creation_tokens"),
            status=row.get("status"),
            started_at=row.get("started_at"),
            completed_at=row.get("completed_at"),
        )


def _aux_totals(attempts: list[dict] | None) -> tuple[int | None, int | None, int | None]:
    """Non-coder, non-reviewer burn summed per bucket across attempts.

    Shared by TaskSummaryOut (board card) and TaskOut (drawer) so the two
    surfaces cannot disagree: the drawer omitted these entirely and
    web/src/cost.js read undefined→0, under-reporting the run by the whole
    planner+utility tier on the surface where a human approves.

    The roles come from ``db.USAGE_ROLES`` (everything but the coder, whose
    columns the caller already reports, and the reviewer, which has its own
    keys) rather than from a literal pair. Naming them here is how the board
    would have kept showing a planner+utility total on the day the supervisor
    and distill roles landed — silently short by two roles on the surface a
    human approves spend from.
    """
    if not attempts:
        return None, None, None
    aux_tiers = [t for t in USAGE_ROLES if t not in ("", "review_")]

    def _auxsum(idx: int) -> int | None:
        keys = [usage_columns_for(t)[idx] for t in aux_tiers]
        vals = [sum(a.get(k) or 0 for k in keys) for a in attempts]
        return sum(vals) if any(v > 0 for v in vals) else None

    return (_auxsum(0), _auxsum(1), _auxsum(2))


class TaskOut(BaseModel):
    id: str
    # SCRUM-16: the scheduler has this task actively in-flight right now —
    # same contract as TaskSummaryOut.claimed (SCRUM-15); set only by the
    # detail endpoint, which is the one path with a scheduler in reach.
    claimed: bool = False
    external_id: str | None = None
    source: str
    title: str
    description: str | None = None
    status: str
    kind: str = "feature"
    priority: str | None = None
    acceptance_criteria: list[str] = []
    repo_path: str | None = None
    blocker: dict | None = None
    context: dict | None = None
    parent_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    attempts: list[AttemptOut] = []
    # Per-task cost meter: the three axes, surfaced together. One number cannot
    # answer "what did this cost" — the axes below are priced differently and a
    # surface showing only one of them misreports the bill.
    total_tokens: int | None = None
    # Cache-read is 90%+ of real burn (C1); a meter showing tokens_used alone
    # under-reported a 33M-token task as "121.5k tok".
    total_cache_read: int | None = None
    # Cache-CREATION is full-price fresh work, and it was invisible to every cost surface:
    # a per-task cost priced two of the three buckets, so the task rows summed to less than
    # the lifetime figure on the same page.
    total_cache_creation: int | None = None
    # The reviewer's burn on this task — the gate is not free, and pricing only the coder
    # under-reported every task by the whole adversarial review.
    total_review_tokens: int | None = None
    total_review_cache_creation: int | None = None
    total_review_cache_read: int | None = None
    # The planner + utility tiers' burn. TaskSummaryOut has carried these since
    # B2 #5/#6; TaskOut did not, so the drawer's nine-bucket cost silently
    # dropped them (undefined→0 in web/src/cost.js) and read ~10% lower than
    # the board card for the same task.
    total_aux_tokens: int | None = None
    total_aux_cache_read: int | None = None
    total_aux_cache_creation: int | None = None
    wall_seconds: float | None = None
    attempt_count: int = 0
    #: Why this task failed, taken from its last attempt that recorded a reason.
    #:
    #: `failure_reason` is a column on ATTEMPTS, never on tasks — so the drawer's
    #: "Why it failed" banner (`web/src/SlideOver.jsx`, gated on
    #: `task.failure_reason`) could never render: the field was not in the
    #: payload. `web/e2e/failure-reason.mjs` passes anyway because it builds its
    #: own task object with the field already set, so it pins the COMPONENT's
    #: contract and never exercises the API that has to satisfy it.
    #:
    #: Not populated for an operator cancel — those are stored as `failed` plus a
    #: `cancel_reason` (`cli/commands.py`, `api/app.py`), and the last attempt's
    #: reason would explain a stop nobody asked about under a heading that says
    #: the task failed.
    failure_reason: str | None = None
    # Mirrors TaskSummaryOut.escalation_attempts/escalation_tokens — the
    # `failure_reason` comment above records exactly the bug caused by a
    # component-only field, so this is drawer parity, not new surface.
    escalation_attempts: int | None = None
    escalation_tokens: int | None = None

    @classmethod
    def from_task(cls, task: Task, attempts: list[dict]) -> "TaskOut":
        toks = [a.get("tokens_used") or 0 for a in (attempts or [])]
        total_tokens = sum(toks) if any(t > 0 for t in toks) else None
        cread = [a.get("cache_read_tokens") or 0 for a in (attempts or [])]
        total_cache_read = sum(cread) if any(c > 0 for c in cread) else None
        ccre = [a.get("cache_creation_tokens") or 0 for a in (attempts or [])]
        total_cache_creation = sum(ccre) if any(c > 0 for c in ccre) else None
        def _sum(key: str) -> int | None:
            vals = [a.get(key) or 0 for a in (attempts or [])]
            return sum(vals) if any(v > 0 for v in vals) else None
        total_review_tokens = _sum("review_tokens_used")
        total_review_cache_creation = _sum("review_cache_creation_tokens")
        total_review_cache_read = _sum("review_cache_read_tokens")
        total_aux_tokens, total_aux_cache_read, total_aux_cache_creation = _aux_totals(attempts)
        escalation_attempts = None
        escalation_tokens = None
        if task.blocker and isinstance(task.blocker, dict):
            _lat = task.blocker.get("escalation_latency") or {}
            escalation_attempts = _lat.get("attempts_before_escalation")
            escalation_tokens = _lat.get("tokens_before_escalation")
        return cls(
            id=task.id,
            external_id=task.external_id,
            source=task.source,
            title=task.title,
            description=task.description,
            status=task.status.value,
            kind=task.kind,
            priority=task.priority,
            acceptance_criteria=task.acceptance_criteria,
            repo_path=task.repo_path,
            blocker=task.blocker,
            context=task.context,
            parent_id=task.parent_id,
            created_at=task.created_at,
            updated_at=task.updated_at,
            attempts=[AttemptOut.from_row(a) for a in attempts],
            total_tokens=total_tokens,
            total_cache_read=total_cache_read,
            total_cache_creation=total_cache_creation,
            total_review_tokens=total_review_tokens,
            total_review_cache_creation=total_review_cache_creation,
            total_review_cache_read=total_review_cache_read,
            total_aux_tokens=total_aux_tokens,
            total_aux_cache_read=total_aux_cache_read,
            total_aux_cache_creation=total_aux_cache_creation,
            wall_seconds=_wall_seconds(task.created_at, _last_activity(task, attempts)),
            attempt_count=len(attempts) if attempts else 0,
            failure_reason=_failure_reason(task, attempts),
            escalation_attempts=escalation_attempts,
            escalation_tokens=escalation_tokens,
        )


def _failure_reason(task: Task, attempts: list[dict]) -> str | None:
    """The reason to show under "Why it failed", or None to show nothing.

    Reads the LAST attempt that recorded one, ordered by `attempt_number` and
    falling back to list order when that is absent, because a retry's reason is
    the one that explains the task's final state.

    Two cases deliberately return None rather than a plausible-looking string:

    * the task did not fail — a task that failed attempt 1 and passed attempt 2
      is not a failed task, and its old reason under that heading is a lie;
    * an operator CANCEL, which is stored as `failed` plus a `cancel_reason`
      (`cli/commands.py`, `api/app.py`). The last attempt's reason there
      describes work that was interrupted, not a failure anyone diagnosed. The
      same distinction the read sites in `cli/commands.py` and `api/app.py`
      already make, and that `core/metrics.py` was missing.
    """
    if task.status.value != "failed":
        return None
    if (task.context or {}).get("cancel_reason") is not None:
        return None
    ordered = sorted(
        (a for a in (attempts or []) if str(a.get("failure_reason") or "").strip()),
        key=lambda a: a.get("attempt_number") or 0,
    )
    return str(ordered[-1]["failure_reason"]).strip() if ordered else None


def _parse_ts(s: str | None):
    """Parse an ISO timestamp string to an aware datetime, treating a naive
    string (the realistic corruption shape, e.g. '2026-07-26 06:11:56') as
    UTC rather than raising. Returns None on any parse failure."""
    from datetime import datetime, timezone

    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _last_activity(task: "Task", attempts: list[dict] | None) -> str | None:
    """Return the most recent timestamp across task.updated_at and attempt
    timestamps.  Used for the board card's 'last activity' line."""
    candidates = [task.updated_at or ""]
    for a in (attempts or []):
        candidates.append(a.get("completed_at") or "")
        candidates.append(a.get("started_at") or "")
    parsed = [(_parse_ts(c), c) for c in candidates]
    dated = [(dt, c) for dt, c in parsed if dt is not None]
    if dated:
        return max(dated, key=lambda p: p[0])[1] or None
    return max(candidates) or None  # preserve prior behavior when nothing parses


def _wall_seconds(created_at: str | None, last_activity: str | None) -> float | None:
    """Wall-clock seconds a task has been alive (created → last activity).

    The time half of the per-task cost meter (tokens is the other half). Cost is
    best-effort telemetry: any missing or unparseable timestamp returns None
    rather than raising. Naive timestamps (a corrupt row missing tz info) are
    treated as UTC rather than erroring, so one bad row can't 500 the board.
    """
    if not created_at or not last_activity:
        return None
    a, b = _parse_ts(created_at), _parse_ts(last_activity)
    if a is None or b is None:
        return None
    try:
        return max(0.0, (b - a).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _coerce_round_count(value) -> int:
    """Context values come from JSON the watcher wrote, but corruption (or a
    future writer bug) must degrade to 0 — never 500 the whole board."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


class TaskSummaryOut(BaseModel):
    id: str
    external_id: str | None = None
    source: str
    title: str
    status: str
    kind: str = "feature"
    priority: str | None = None
    pr_url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    # Richer fields so the board card is useful without clicking through.
    repo_name: str | None = None
    description_short: str | None = None
    attempt_count: int = 0
    last_turns: int | None = None
    blocker_question: str | None = None
    blocker_category: str | None = None
    blocker_wake_condition: str | None = None
    # {attempts,tokens}_before_escalation from task.blocker["escalation_latency"];
    # None when unmeasured (fail-open telemetry, see orchestrator._raise_blocker)
    # — the board/CLI must render nothing, never a fabricated 0.
    escalation_attempts: int | None = None
    escalation_tokens: int | None = None
    # The human explicitly stopped this parked task (/pause on an already-parked
    # task, or /reply with terminal:true) — durable per wake.py, which already
    # skips these in the wake sweep. Silences "N need you" for the row without
    # moving it out of its lane (see isNeedsYou / actionHint / narrativeFor).
    blocker_human_stopped: bool = False
    last_activity: str | None = None
    backend: str | None = None  # the coder backend THIS task asked for, if any
    total_tokens: int | None = None
    total_cache_read: int | None = None
    total_cache_creation: int | None = None
    # B2 #12: TaskTable priced these but the list endpoint never sent them —
    # undefined→0 silently dropped the whole review gate from every row while
    # the lifetime tile (same page) included it. Rows summed to less than the
    # total, the exact surfaces-disagree class the cost model was rebuilt for.
    total_review_tokens: int | None = None
    total_review_cache_read: int | None = None
    total_review_cache_creation: int | None = None
    # B2 #5/#6 (review #2): planning + utility burn on their own columns —
    # summed here so the task row prices the WHOLE run, not just coder+review.
    total_aux_tokens: int | None = None
    total_aux_cache_read: int | None = None
    total_aux_cache_creation: int | None = None
    wall_seconds: float | None = None  # created → last activity; time half of the cost meter
    parent_id: str | None = None
    has_spec: bool = False
    live_status: str | None = None
    subtask_progress: str | None = None
    # A task an operator cancelled ends in FAILED status but is not a capability
    # failure — set so Stats can keep it out of the success-rate denominator.
    cancelled: bool = False
    # B2 #19: approve() records approved_at but the task STAYS in
    # awaiting_approval until the merge lands, so an approved PR looked
    # identical to an un-reviewed one and kept shouting in "N need you" — a
    # second operator (or the Electron window) would re-review it.
    approved_at: str | None = None
    # SCRUM-15: the scheduler has this task actively in-flight RIGHT NOW —
    # distinct from `status` being one of the "active" values, which merely
    # means it's eligible to run. `from_task` has no scheduler access, so this
    # defaults False; `_board_tasks` (the only path with a scheduler) sets it
    # from `scheduler.inflight`.
    claimed: bool = False
    # SCRUM-42: >0 while the wake watcher is cycling a CONFLICTING PR through
    # bounded rebase send-backs (SCRUM-41's context key, mirrored verbatim).
    # The card renders "resolving merge conflict" from THIS, never by
    # string-matching feedback text. Reset to 0 by the rung on a definite
    # MERGEABLE, so the badge clears itself.
    pr_conflict_rounds: int = 0
    # The configured blockers.max_pr_conflict_rounds bound, surfaced so the
    # badge renders "round N/M" with real data. 0 = unknown (older callers),
    # which the frontend renders as a bare round number.
    max_pr_conflict_rounds: int = 0
    # Board lane, decided by core.lanes.lane_for so the web UI and any other
    # client cannot disagree about it (the JS used to own this outright).
    # `from_task` has no reason to route a summary that is not going to a board,
    # so this defaults to None and `_board_tasks` fills it; the frontend falls
    # back to its own copy of the routing when the field is absent.
    lane: str | None = None

    @classmethod
    def from_task(
        cls,
        task: Task,
        pr_url: str | None = None,
        attempts: list[dict] | None = None,
        max_pr_conflict_rounds: int = 0,
    ) -> "TaskSummaryOut":
        repo_name = task.repo_path.rstrip("/").rsplit("/", 1)[-1] if task.repo_path else None
        desc_short = (task.description or "")[:120] or None
        attempt_count = len(attempts) if attempts else 0
        last_turns = None
        if attempts:
            for a in reversed(attempts):
                if a.get("turns_used"):
                    last_turns = a["turns_used"]
                    break
        blocker_q = None
        blocker_cat = None
        blocker_wake = None
        blocker_stopped = False
        escalation_attempts = None
        escalation_tokens = None
        if task.blocker and isinstance(task.blocker, dict):
            blocker_q = task.blocker.get("question")
            blocker_cat = task.blocker.get("category")
            blocker_wake = task.blocker.get("wake_condition")
            blocker_stopped = bool(task.blocker.get("human_stopped"))
            _lat = task.blocker.get("escalation_latency") or {}
            escalation_attempts = _lat.get("attempts_before_escalation")
            escalation_tokens = _lat.get("tokens_before_escalation")
        # Per-task backend from config dict.
        task_backend = None
        if hasattr(task, "config") and isinstance(task.config, dict):
            task_backend = task.config.get("backend")
        # Sum tokens across all attempts.
        total_tokens = None
        total_cache_read = None
        total_cache_creation = None
        if attempts:
            toks = [a.get("tokens_used") or 0 for a in attempts]
            total_tokens = sum(toks) if any(t > 0 for t in toks) else None
            cread = [a.get("cache_read_tokens") or 0 for a in attempts]
            total_cache_read = sum(cread) if any(c > 0 for c in cread) else None
            ccre = [a.get("cache_creation_tokens") or 0 for a in attempts]
            total_cache_creation = sum(ccre) if any(c > 0 for c in ccre) else None
        total_review_tokens = None
        total_review_cache_read = None
        total_review_cache_creation = None
        if attempts:
            def _rsum(key: str) -> int | None:
                vals = [a.get(key) or 0 for a in attempts]
                return sum(vals) if any(v > 0 for v in vals) else None
            total_review_tokens = _rsum("review_tokens_used")
            total_review_cache_read = _rsum("review_cache_read_tokens")
            total_review_cache_creation = _rsum("review_cache_creation_tokens")
        total_aux_tokens, total_aux_cache_read, total_aux_cache_creation = _aux_totals(attempts)
        return cls(
            id=task.id,
            external_id=task.external_id,
            source=task.source,
            title=task.title,
            status=task.status.value,
            kind=task.kind,
            priority=task.priority,
            pr_url=pr_url,
            created_at=task.created_at,
            updated_at=task.updated_at,
            repo_name=repo_name,
            description_short=desc_short,
            attempt_count=attempt_count,
            last_turns=last_turns,
            blocker_question=blocker_q,
            blocker_category=blocker_cat,
            blocker_wake_condition=blocker_wake,
            blocker_human_stopped=blocker_stopped,
            escalation_attempts=escalation_attempts,
            escalation_tokens=escalation_tokens,
            last_activity=_last_activity(task, attempts),
            backend=task_backend,
            total_tokens=total_tokens,
            total_cache_read=total_cache_read,
            total_cache_creation=total_cache_creation,
            total_review_tokens=total_review_tokens,
            total_review_cache_read=total_review_cache_read,
            total_review_cache_creation=total_review_cache_creation,
            total_aux_tokens=total_aux_tokens,
            total_aux_cache_read=total_aux_cache_read,
            total_aux_cache_creation=total_aux_cache_creation,
            wall_seconds=_wall_seconds(task.created_at, _last_activity(task, attempts)),
            parent_id=task.parent_id,
            has_spec=bool((task.context or {}).get("spec")),
            cancelled=bool((task.context or {}).get("cancel_reason")),
            approved_at=(task.context or {}).get("approved_at"),
            pr_conflict_rounds=_coerce_round_count(
                (task.context or {}).get("pr_conflict_rounds")),
            max_pr_conflict_rounds=max_pr_conflict_rounds,
        )


class CreateTaskRequest(BaseModel):
    title: str
    description: str | None = None
    repo_path: str | None = None
    project_id: str | None = None
    kind: str = "feature"
    priority: str = "medium"
    acceptance_criteria: list[str] = []
    backend: str | None = None  # claude | codex | local; None = worker.backend
    # "board" (typed) or "jira" (Import from Jira) — any other value falls back
    # to "board" server-side. Task.source already models this (intake/jira.py's
    # poller stamps "jira" too); this just lets the web create path pick it.
    source: str = "board"
    # Jira dedup key (SCRUM-32); only honored when source == "jira" — trimmed
    # and length-capped server-side in the create-task handler.
    external_id: str | None = None
    # The branch this task's work should branch FROM (PR-001).
    #
    # Until this existed, a task branched from whatever the primary checkout
    # happened to have checked out (`orchestrator.py`: `base = ctx["base_branch"]
    # or main_repo.current_branch()`), and NO API or GUI field could pin it. The
    # mismatch surfaced only as a non-blocking warning at PR time — after the run
    # was paid for. The documented workaround was "put the checkout on main
    # before filing anything", i.e. a precondition a human has to remember every
    # time, which is not a fix.
    #
    # Left as None, the base is now the PROJECT'S DEFAULT BRANCH — the profile's
    # declared `default_branch`, else the remote's `origin/HEAD`, and only for a
    # repo with neither (no origin: the bench's fixtures, most tests) the
    # checkout's branch. `orchestrator._implicit_base_branch` is the one place
    # that decides it, for the primary repo and for linked repos alike.
    #
    # 🔴 THIS IS HALF OF PR-001, AND THE HALF A HUMAN CANNOT REACH.
    # An earlier version of this comment said "the composer offers it, so a
    # person filing a ticket can see and choose it". It does not — there is no
    # `base_branch` anywhere in `web/src`, an architecture audit proved it with
    # a positive control, and PR-001 is explicit that this is "A GUI fix, not a
    # doc fix". What HAS changed (2026-08-09) is the default that field would
    # have offered: the run-book precondition "put the primary checkout on
    # `main` before filing anything" is retired — the checkout's branch is no
    # longer inherited, so a stale checkout can no longer send a run to a base
    # `gh pr create` refuses ("No commits between <stale> and no-human/<id>").
    # What remains for PR-001: the composer field itself, for the person who
    # wants a base OTHER than the default (a stacked PR onto a colleague's
    # branch), defaulting to the resolved default branch and editable.
    base_branch: str | None = None
    # GAP 1 — the optional human plan-approval gate. Default False keeps every
    # existing caller (bench, `nh repo new`, the composer) on today's
    # unattended path. Ignored for source == "jira": an imported ticket is not
    # a person choosing to sit in front of the run, and the Jira poller (which
    # never reaches this endpoint) must behave identically to an import made
    # through it.
    plan_approval: bool = False


class ImportedInfo(BaseModel):
    """SCRUM-18 — the accidental-re-import trap. A tiny board-side lookup
    attached to a browse row when the ticket already has a board task,
    matched by (source, external_id) against the local tasks store. Never the
    full Task shape (models.py's own TaskOut/TaskSummaryOut), no secrets."""
    task_id: str        # the board task id, for binding — never the full task shape
    status: str          # board task status value (e.g. "done", "implementing")
    count: int = 1        # >1 signals duplicate external_ids (data-integrity warning)


class TrackerIssueOut(BaseModel):
    """One row in the Backlog page's ticket list — never the full Task shape
    the adapters' ``normalize`` builds, and never a secret.

    ONE shape for both trackers. The page lists Jira and Linear side by side,
    and the two rows differ in nothing a reader of this list cares about, so a
    second near-identical model would only be a way for them to drift."""
    # Which tracker this row came from: "jira" or "linear". The page needs it
    # to route the detail fetch and to stamp the created task's `source`, and
    # dedupe keys on (source, external_id) — so a Jira NO-1 and a Linear NO-1
    # are two different tickets and must not claim each other's board task.
    tracker: str = "jira"
    key: str
    summary: str
    status: str | None = None
    assignee: str | None = None
    updated: str | None = None
    url: str
    description: str = ""
    # SCRUM-18: set when a board task already exists for this issue's key.
    imported: ImportedInfo | None = None


class SendBackRequest(BaseModel):
    message: str


class ShippedRequest(BaseModel):
    # SCRUM-56: operator testimony, never verified — see the endpoint docstring.
    sha: str
    note: str | None = None


class CancelRequest(BaseModel):
    # Optional: the board's cancel button always posts this (reason may be
    # None when the operator left it blank); `nh` CLI callers and any other
    # no-body POST still work — see cancel_task's default in api/app.py.
    reason: str | None = None


class LandedOverrideRequest(BaseModel):
    # Unlike ShippedRequest, `sha` here IS verified — commit_is_ancestor
    # against the task's recorded base_branch — and `justification` is
    # required (non-empty, enforced server-side): see
    # blockers/landed_override.py's approve_landed_override.
    sha: str
    justification: str


class SaveIntegrationConfigRequest(BaseModel):
    fields: dict[str, str] = {}


class TelemetryConsentRequest(BaseModel):
    """Settings > Usage insights toggle. `enabled` is the ONLY field: the
    anonymous instance id is minted server-side on first enable — never
    accepted from the browser."""
    enabled: bool


class IntegrationSetupRequest(BaseModel):
    """The onboarding "Connect your tools" step's payload for ONE integration:
    its non-secret settings only (bools, text, string lists).

    `str` is deliberately absent from the value union's scalar side except as
    text — there is no field here that could carry a credential, because the
    server re-checks every key against
    ``integrations.assert_config_safe_field`` before writing. Secrets are
    never posted to this route; the wizard only NAMES the ~/.no_human/.env
    variable they belong in."""
    values: dict[str, bool | str | list[str]] = {}


class ReplyRequest(BaseModel):
    # Optional ONLY because `choose` can carry the answer instead: picking
    # option N answers with N's label, which is exactly what `nh reply
    # --choose N` does. The board's one-click option button posts `{choose}`
    # with no `answer` at all, so a required `answer` rejected every one of
    # those with a 422 — the buttons could not be clicked. An empty body is
    # still 422 (the validator below), so nothing that was rejected before for
    # being answerless is accepted now.
    answer: str | None = None
    # 1-based index into the blocker's options. When set, the chosen option's
    # action is applied — the only path by which one ever runs.
    choose: int | None = None

    @model_validator(mode="after")
    def _answer_or_choose(self) -> "ReplyRequest":
        if self.answer is None and self.choose is None:
            raise ValueError("give an answer or choose an option")
        # A BLANK answer is not an answer. `{"answer": ""}` used to be stored
        # as a real reply: at the plan gate it recorded `state=correcting,
        # correction=""`, which stranded the task in PLANNING with no worker
        # (nothing claimed it) and 409'd every further reply, and it would
        # otherwise be handed to the planner as a binding correction — burning
        # the single re-plan allowance on nothing.
        #
        # Scoped to a FREE-TEXT reply. `{"answer": "", "choose": N}` is what
        # `nh reply --choose` and older board builds post, and there the answer
        # is replaced by option N's label before anything reads it — the field
        # is inert, so rejecting it would break a working call for no gain.
        if self.choose is None and self.answer is not None and not self.answer.strip():
            raise ValueError("answer must not be blank")
        return self


class BoardPayload(BaseModel):
    tasks: list[TaskSummaryOut]


class GrillStepRequest(BaseModel):
    title: str
    description: str | None = None
    repo_path: str | None = None
    project_id: str | None = None
    qa_history: list[dict] = []


class GrillQuestionOut(BaseModel):
    type: str = "question"
    question: str
    suggestions: list[str]
    round: int


class GrillResultOut(BaseModel):
    type: str = "done"
    title: str
    description: str
    acceptance_criteria: list[str]


class ProjectOut(BaseModel):
    id: str
    name: str
    repo_paths: list[str]
    primary_repo: str | None = None
    test_layers: list[dict[str, Any]] = []
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_project(cls, p: Any) -> "ProjectOut":
        layers = []
        raw = getattr(p, "test_layers", "[]")
        if raw:
            try:
                layers = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                pass
        return cls(
            id=p.id, name=p.name, repo_paths=p.repo_paths,
            primary_repo=p.primary_repo, test_layers=layers,
            created_at=p.created_at, updated_at=p.updated_at,
        )


class CreateProjectRequest(BaseModel):
    name: str
    repo_paths: list[str] = []
    primary_repo: str | None = None


class UpdateProjectRequest(BaseModel):
    name: str | None = None
    repo_paths: list[str] | None = None
    primary_repo: str | None = None
    test_layers: list[dict[str, Any]] | None = None
