"""Pydantic response models for the no_human board API."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

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
            turns_used=row.get("turns_used"),
            tokens_used=row.get("tokens_used"),
            cache_read_tokens=row.get("cache_read_tokens"),
            cache_creation_tokens=row.get("cache_creation_tokens"),
            status=row.get("status"),
            started_at=row.get("started_at"),
            completed_at=row.get("completed_at"),
        )


class TaskOut(BaseModel):
    id: str
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
    # Per-task cost meter (internal note #1): the three axes, surfaced together.
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
    wall_seconds: float | None = None
    attempt_count: int = 0

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
            wall_seconds=_wall_seconds(task.created_at, _last_activity(task, attempts)),
            attempt_count=len(attempts) if attempts else 0,
        )


def _last_activity(task: "Task", attempts: list[dict] | None) -> str | None:
    """Return the most recent timestamp across task.updated_at and attempt
    timestamps.  Used for the board card's 'last activity' line."""
    candidates = [task.updated_at or ""]
    for a in (attempts or []):
        candidates.append(a.get("completed_at") or "")
        candidates.append(a.get("started_at") or "")
    return max(candidates) or None


def _wall_seconds(created_at: str | None, last_activity: str | None) -> float | None:
    """Wall-clock seconds a task has been alive (created → last activity).

    The time half of the per-task cost meter (tokens is the other half). Cost is
    best-effort telemetry: any missing or unparseable timestamp returns None
    rather than raising.
    """
    if not created_at or not last_activity:
        return None
    from datetime import datetime

    def _p(s: str):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    a, b = _p(created_at), _p(last_activity)
    if a is None or b is None:
        return None
    return max(0.0, (b - a).total_seconds())


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
    last_activity: str | None = None
    backend: str | None = None  # "claude" (single in-process backend)
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

    @classmethod
    def from_task(
        cls,
        task: Task,
        pr_url: str | None = None,
        attempts: list[dict] | None = None,
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
        if task.blocker and isinstance(task.blocker, dict):
            blocker_q = task.blocker.get("question")
            blocker_cat = task.blocker.get("category")
            blocker_wake = task.blocker.get("wake_condition")
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
        total_aux_tokens = None
        total_aux_cache_read = None
        total_aux_cache_creation = None
        if attempts:
            def _rsum(key: str) -> int | None:
                vals = [a.get(key) or 0 for a in attempts]
                return sum(vals) if any(v > 0 for v in vals) else None
            total_review_tokens = _rsum("review_tokens_used")
            total_review_cache_read = _rsum("review_cache_read_tokens")
            total_review_cache_creation = _rsum("review_cache_creation_tokens")
            def _auxsum(a_key, b_key):
                vals = [(a.get(a_key) or 0) + (a.get(b_key) or 0) for a in attempts]
                return sum(vals) if any(v > 0 for v in vals) else None
            total_aux_tokens = _auxsum("plan_tokens_used", "utility_tokens_used")
            total_aux_cache_read = _auxsum("plan_cache_read_tokens", "utility_cache_read_tokens")
            total_aux_cache_creation = _auxsum("plan_cache_creation_tokens", "utility_cache_creation_tokens")
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
        )


class CreateTaskRequest(BaseModel):
    title: str
    description: str | None = None
    repo_path: str | None = None
    project_id: str | None = None
    kind: str = "feature"
    priority: str = "medium"
    acceptance_criteria: list[str] = []
    backend: str | None = None  # "claude"; None = use global config
    # "board" (typed) or "jira" (Import from Jira) — any other value falls back
    # to "board" server-side. Task.source already models this (intake/jira.py's
    # poller stamps "jira" too); this just lets the web create path pick it.
    source: str = "board"


class JiraIssueOut(BaseModel):
    """One row in the Import-from-Jira browse/pick list — never the full Task
    shape ``JiraAdapter.normalize`` builds, and never a secret."""
    key: str
    summary: str
    status: str | None = None
    assignee: str | None = None
    updated: str | None = None
    url: str
    description: str = ""


class SendBackRequest(BaseModel):
    message: str


class SaveIntegrationConfigRequest(BaseModel):
    fields: dict[str, str] = {}


class ReplyRequest(BaseModel):
    answer: str
    # 1-based index into the blocker's options. When set, the chosen option's
    # action is applied — the only path by which one ever runs.
    choose: int | None = None


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
