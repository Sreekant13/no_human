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
    created_at: str | None = None
    updated_at: str | None = None
    attempts: list[AttemptOut] = []

    @classmethod
    def from_task(cls, task: Task, attempts: list[dict]) -> "TaskOut":
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
            created_at=task.created_at,
            updated_at=task.updated_at,
            attempts=[AttemptOut.from_row(a) for a in attempts],
        )


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
        )


class CreateTaskRequest(BaseModel):
    title: str
    description: str | None = None
    repo_path: str | None = None
    kind: str = "feature"
    priority: str = "medium"
    acceptance_criteria: list[str] = []


class SendBackRequest(BaseModel):
    message: str


class ReplyRequest(BaseModel):
    answer: str


class BoardPayload(BaseModel):
    tasks: list[TaskSummaryOut]
