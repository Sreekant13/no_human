"""Autonomy telemetry (megaplan P0).

Read-only metrics that measure how close no_human is to its North Star: the
only human touchpoints should be starting the site and reviewing/merging the
final PR. Every task that ends in ESCALATED / AWAITING_INPUT / BLOCKED is a
"mid-flight touchpoint" — a human was pulled in before the PR. A task that
reaches AWAITING_APPROVAL or DONE reached a reviewable PR (success).

Pure computation over the tasks/attempts tables — no side effects — so it is
unit-testable and reusable by both the CLI (`nh autonomy`) and the API.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .task import Task, TaskStatus

# Statuses that mean a human was pulled in mid-flight (before a PR existed).
_TOUCHPOINT_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.ESCALATED,
    TaskStatus.AWAITING_INPUT,
    TaskStatus.BLOCKED,
})

# Statuses that mean the task reached a reviewable PR (the sanctioned touchpoint).
_PR_REACHED_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.AWAITING_APPROVAL,
    TaskStatus.DONE,
})

# A finished-or-parked task (excludes tasks still actively being worked, which
# shouldn't count for/against the autonomy ratios yet).
_ACTIVE_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.PENDING,
    TaskStatus.CONTEXT,
    TaskStatus.PLANNING,
    TaskStatus.IMPLEMENTING,
    TaskStatus.REVIEWING,
    TaskStatus.TESTING,
    TaskStatus.COMPOUND_PARENT,
    TaskStatus.PAUSED_QUOTA,
})

_TURN_EXHAUSTION_RE = re.compile(r"max[_ ]?turns|did not complete|turn budget",
                                 re.IGNORECASE)


@dataclass
class AutonomyReport:
    window_days: int | None = None
    total_tasks: int = 0
    settled_tasks: int = 0          # not actively in-flight
    pr_reached: int = 0
    touchpoint_tasks: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    blocker_categories: dict[str, int] = field(default_factory=dict)
    turn_exhaustion_empty: int = 0

    @property
    def pr_reached_rate(self) -> float | None:
        return (self.pr_reached / self.settled_tasks) if self.settled_tasks else None

    @property
    def touchpoint_rate(self) -> float | None:
        return (self.touchpoint_tasks / self.settled_tasks) if self.settled_tasks else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days,
            "total_tasks": self.total_tasks,
            "settled_tasks": self.settled_tasks,
            "pr_reached": self.pr_reached,
            "touchpoint_tasks": self.touchpoint_tasks,
            "pr_reached_rate": self.pr_reached_rate,
            "touchpoint_rate": self.touchpoint_rate,
            "by_status": self.by_status,
            "blocker_categories": self.blocker_categories,
            "turn_exhaustion_empty": self.turn_exhaustion_empty,
        }


def _within_window(task: Task, cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    created = task.created_at
    if not created:
        return True
    try:
        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= cutoff


def _is_empty_diff(diff: Any) -> bool:
    return not diff or not str(diff).strip()


async def compute_autonomy_metrics(
    store: Any, *, days: int | None = None,
) -> AutonomyReport:
    """Compute autonomy telemetry over all tasks (optionally windowed by
    ``days`` on task creation time). Read-only."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)) if days else None
    tasks = [t for t in await store.list_tasks() if _within_window(t, cutoff)]

    report = AutonomyReport(window_days=days, total_tasks=len(tasks))
    status_counter: Counter[str] = Counter()
    blocker_counter: Counter[str] = Counter()

    for t in tasks:
        status_counter[t.status.value] += 1
        if t.status in _TOUCHPOINT_STATUSES:
            report.touchpoint_tasks += 1
        if t.status in _PR_REACHED_STATUSES:
            report.pr_reached += 1
        if t.status not in _ACTIVE_STATUSES:
            report.settled_tasks += 1
        if t.blocker:
            cat = str(t.blocker.get("category") or "UNKNOWN")
            blocker_counter[cat] += 1

    # Turn-exhaustion-with-empty-diff attempts (megaplan B5 baseline metric).
    for t in tasks:
        try:
            attempts = await store.list_attempts(t.id)
        except Exception:  # noqa: BLE001 — telemetry must never crash
            continue
        for a in attempts:
            reason = a.get("failure_reason") or ""
            if _TURN_EXHAUSTION_RE.search(reason) and _is_empty_diff(a.get("diff")):
                report.turn_exhaustion_empty += 1

    report.by_status = dict(status_counter)
    report.blocker_categories = dict(blocker_counter)
    return report
