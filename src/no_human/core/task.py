"""Task model, status enum, and the legal state-transition map (PLAN.md 4.3)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    PENDING = "pending"
    CONTEXT = "context"
    PLANNING = "planning"
    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    TESTING = "testing"
    AWAITING_APPROVAL = "awaiting_approval"
    # compound parent — parked while sub-tasks execute (frees worker slot)
    COMPOUND_PARENT = "compound_parent"
    # off-ramps
    BLOCKED = "blocked"
    AWAITING_INPUT = "awaiting_input"
    PAUSED_QUOTA = "paused_quota"
    ESCALATED = "escalated"
    DONE = "done"
    FAILED = "failed"


# The happy-path spine, in order. Used to advance the loop linearly.
MAIN_FLOW: tuple[TaskStatus, ...] = (
    TaskStatus.PENDING,
    TaskStatus.CONTEXT,
    TaskStatus.PLANNING,
    TaskStatus.IMPLEMENTING,
    TaskStatus.REVIEWING,
    TaskStatus.TESTING,
    TaskStatus.AWAITING_APPROVAL,
    TaskStatus.DONE,
)

TERMINAL_STATES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.DONE, TaskStatus.FAILED}
)

# Off-ramp states reachable from any active working state (Part 22).
_OFF_RAMPS: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.BLOCKED,
        TaskStatus.AWAITING_INPUT,
        TaskStatus.PAUSED_QUOTA,
        TaskStatus.ESCALATED,
        TaskStatus.FAILED,
    }
)

_ACTIVE: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.CONTEXT,
        TaskStatus.PLANNING,
        TaskStatus.IMPLEMENTING,
        TaskStatus.REVIEWING,
        TaskStatus.TESTING,
    }
)


def _allowed_transitions() -> dict[TaskStatus, frozenset[TaskStatus]]:
    """Build the legal transition map.

    Rules:
    - Each main-flow state may advance to the next main-flow state.
    - Any active working state may drop to any off-ramp.
    - Parked off-ramps (blocked/awaiting_input/paused_quota) may resume to any
      active state (the watcher / a human reply re-enters mid-flow).
    - escalated may resume too (after a human answers); failed is terminal.
    - awaiting_approval -> done (human approves) or implementing (sent back).
    """
    table: dict[TaskStatus, set[TaskStatus]] = {s: set() for s in TaskStatus}

    for i, state in enumerate(MAIN_FLOW[:-1]):
        table[state].add(MAIN_FLOW[i + 1])

    for state in _ACTIVE:
        table[state] |= _OFF_RAMPS

    # PENDING can fail immediately (e.g. no repo_path set).
    table[TaskStatus.PENDING].add(TaskStatus.FAILED)

    # code_review tasks skip planning/implementing and go straight to reviewing.
    table[TaskStatus.CONTEXT].add(TaskStatus.REVIEWING)
    # code_review completes directly after review (no approval gate).
    table[TaskStatus.REVIEWING].add(TaskStatus.DONE)

    # reviewing can loop back to implementing when the gate fails (within bounds)
    table[TaskStatus.REVIEWING].add(TaskStatus.IMPLEMENTING)
    table[TaskStatus.TESTING].add(TaskStatus.IMPLEMENTING)

    # approval gate
    table[TaskStatus.AWAITING_APPROVAL].add(TaskStatus.IMPLEMENTING)  # sent back
    table[TaskStatus.AWAITING_APPROVAL] |= _OFF_RAMPS

    resumable = {
        TaskStatus.BLOCKED,
        TaskStatus.AWAITING_INPUT,
        TaskStatus.PAUSED_QUOTA,
        TaskStatus.ESCALATED,
    }
    for state in resumable:
        table[state] |= _ACTIVE
        table[state].add(TaskStatus.FAILED)
        table[state].add(TaskStatus.PENDING)  # LeadAgent unblocks dep-gated sub-tasks

    # Compound parent: implementing → compound_parent (park),
    # compound_parent → done / failed / escalated (scheduler callback).
    table[TaskStatus.IMPLEMENTING].add(TaskStatus.COMPOUND_PARENT)
    table[TaskStatus.COMPOUND_PARENT] |= {
        TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.ESCALATED,
    }

    return {k: frozenset(v) for k, v in table.items()}


ALLOWED_TRANSITIONS = _allowed_transitions()


class IllegalTransition(ValueError):
    """Raised when a state change is not in the allowed map."""


def can_transition(src: TaskStatus, dst: TaskStatus) -> bool:
    if src == dst:
        return True
    return dst in ALLOWED_TRANSITIONS.get(src, frozenset())


def assert_transition(src: TaskStatus, dst: TaskStatus) -> None:
    if not can_transition(src, dst):
        raise IllegalTransition(f"{src.value} -> {dst.value} is not allowed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TaskSpec:
    """Structured spec extracted from the planner's output."""
    files_to_change: list[str] = field(default_factory=list)
    approach: str = ""
    test_plan: str = ""
    out_of_scope: list[str] = field(default_factory=list)
    verification: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "files_to_change": self.files_to_change,
            "approach": self.approach,
            "test_plan": self.test_plan,
            "out_of_scope": self.out_of_scope,
            "verification": self.verification,
        }

    @staticmethod
    def from_plan(plan_text: str) -> "TaskSpec":
        """Parse structured sections from the planner's markdown output."""
        import re as _re
        sections: dict[str, str] = {}
        current: str | None = None
        lines: list[str] = []
        for line in plan_text.splitlines():
            m = _re.match(r"^##\s+(.+)", line)
            if m:
                if current:
                    sections[current] = "\n".join(lines).strip()
                current = m.group(1).strip().upper()
                lines = []
            else:
                lines.append(line)
        if current:
            sections[current] = "\n".join(lines).strip()

        def _list_items(text: str) -> list[str]:
            items = []
            for l in text.splitlines():
                s = l.strip()
                # Skip markdown horizontal rules ("---", "***", ...) — these
                # separate sections in the planner's output and would otherwise
                # be mis-parsed as an empty bullet item.
                if _re.match(r"^[-*_]{3,}$", s):
                    continue
                if s.startswith(("-", "*")):
                    item = s.lstrip("-*").strip()
                    if item:
                        items.append(item)
            return items

        def _table_files(text: str) -> list[str]:
            """Extract file paths from the first column of a markdown table.

            The planner sometimes emits FILES TO CHANGE/CREATE as a
            `| File | Change |` table instead of a bullet list.
            """
            files = []
            for l in text.splitlines():
                s = l.strip()
                if not s.startswith("|"):
                    continue
                if _re.match(r"^\|[\s:-]+\|", s):
                    continue  # separator row, e.g. |------|------|
                cells = [c.strip().strip("`").strip() for c in s.strip("|").split("|")]
                if not cells or not cells[0]:
                    continue
                if cells[0].lower() in ("file", "files", "path"):
                    continue  # header row
                files.append(cells[0])
            return files

        files_section = sections.get("FILES TO CHANGE/CREATE", "")
        files_to_change = _list_items(files_section) or _table_files(files_section)

        return TaskSpec(
            files_to_change=files_to_change,
            approach=sections.get("APPROACH", ""),
            test_plan=sections.get("TEST PLAN", ""),
            out_of_scope=_list_items(sections.get("OUT OF SCOPE", "")),
            verification=sections.get("VERIFICATION", ""),
        )


@dataclass
class Task:
    id: str
    source: str
    title: str
    status: TaskStatus = TaskStatus.PENDING
    external_id: str | None = None
    description: str | None = None
    requirements: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    repo_path: str | None = None
    kind: str = "feature"           # WS-A task type: feature|bugfix|ci_fix|traceability|test_gap
    linked_repos: list[str] = field(default_factory=list)  # WS-E: additional repo paths
    blocker: dict[str, Any] | None = None
    wake_check_at: str | None = None
    priority: str = "medium"
    context: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    parent_id: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @staticmethod
    def new(title: str, *, source: str = "freeform", repo_path: str | None = None,
            description: str | None = None, external_id: str | None = None,
            kind: str = "feature", parent_id: str | None = None) -> "Task":
        return Task(
            id=uuid.uuid4().hex,
            source=source,
            title=title,
            description=description,
            repo_path=repo_path,
            external_id=external_id,
            kind=kind,
            parent_id=parent_id,
        )

    def to_row(self) -> dict[str, Any]:
        """Serialize to the SQLite column shape (JSON-encode the lists/dicts)."""
        return {
            "id": self.id,
            "external_id": self.external_id,
            "source": self.source,
            "title": self.title,
            "description": self.description,
            "requirements": json.dumps(self.requirements),
            "acceptance_criteria": json.dumps(self.acceptance_criteria),
            "repo_path": self.repo_path,
            "kind": self.kind,
            "linked_repos": json.dumps(self.linked_repos),
            "status": self.status.value,
            "blocker": json.dumps(self.blocker) if self.blocker else None,
            "wake_check_at": self.wake_check_at,
            "priority": self.priority,
            "context": json.dumps(self.context),
            "plan": json.dumps(self.plan),
            "config": json.dumps(self.config),
            "parent_id": self.parent_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_row(row: dict[str, Any]) -> "Task":
        return Task(
            id=row["id"],
            external_id=row["external_id"],
            source=row["source"],
            title=row["title"],
            description=row["description"],
            requirements=json.loads(row["requirements"] or "[]"),
            acceptance_criteria=json.loads(row["acceptance_criteria"] or "[]"),
            repo_path=row["repo_path"],
            kind=row.get("kind") or "feature",
            linked_repos=json.loads(row.get("linked_repos") or "[]"),
            status=TaskStatus(row["status"]),
            blocker=json.loads(row["blocker"]) if row["blocker"] else None,
            wake_check_at=row["wake_check_at"],
            priority=row["priority"],
            context=json.loads(row["context"] or "{}"),
            plan=json.loads(row["plan"] or "{}"),
            config=json.loads(row["config"] or "{}"),
            parent_id=row.get("parent_id"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
