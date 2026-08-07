"""Autonomy telemetry (megaplan P0).

Read-only metrics that measure how close no_human is to its North Star: the
only human touchpoints should be starting the site and reviewing/merging the
final PR. Every task that ends in ESCALATED / AWAITING_INPUT / BLOCKED is a
"mid-flight touchpoint" — a human was pulled in before the PR.

WHAT `pr_reached` ACTUALLY MEANS, corrected 2026-08-07
-----------------------------------------------------
This docstring used to say "a task that reaches AWAITING_APPROVAL or DONE
reached a reviewable PR (success)". Both halves of that were wrong, and the
second one is why this module got a companion.

It is not "a PR". Three code paths reach AWAITING_APPROVAL and only one of
them opens a pull request: `orchestrator.py`'s PR-open path does, the
"already satisfied — no code change needed" path does not, and the
code-review path (which drafts review comments) does not. So `pr_reached`
counts some tasks that produced no PR at all.

It is not "success" either. Even for the path that does open a PR, the
predicate is a STATUS — `_PR_REACHED_STATUSES` below is the whole test, and
it contains no forge query. Nothing here ever asked whether that PR merged,
was closed unmerged, or is still sitting open. "Opened a pull request" and
"the work landed" were the same number wearing two names.

`compute_pr_outcome_metrics` below answers the question this one does not, by
reading the `pr_outcomes` table (migration 0010). The two are reported side by
side and never blended: see `render_pr_outcome_lines`.

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


@dataclass
class PrOutcomeReport:
    """What became of the PRs, as SEPARATE figures that are never blended.

    There is deliberately no single "success rate" attribute on this class. The
    defect it corrects was one number answering a question nobody had asked, so
    the replacement refuses to be one number: a caller that wants a rate has to
    choose a denominator, and each denominator is named.
    """

    window_days: int | None = None
    #: Tasks in `_PR_REACHED_STATUSES` — the OLD "success" figure, unchanged and
    #: reported next to the new ones rather than replaced by them.
    delivered_tasks: int = 0
    #: Of those, how many have at least one row in `pr_outcomes`.
    tasks_with_recorded_pr: int = 0
    #: Of those, how many have none. MIXED BUCKET, and the report says so: it
    #: holds both "this task legitimately never opens a PR" (already-satisfied,
    #: code-review) and "this task predates the telemetry". Nothing recorded
    #: after the fact can separate them, so nothing pretends to.
    tasks_without_recorded_pr: int = 0
    #: Per-TASK outcome (multi-repo tasks rolled up by `pr_outcome.rollup`),
    #: over `tasks_with_recorded_pr`. Every key always present, including zeros.
    task_outcomes: dict[str, int] = field(default_factory=dict)
    #: Per-PR outcome. Differs from the above when a task opened several PRs.
    pr_outcomes: dict[str, int] = field(default_factory=dict)
    total_prs: int = 0

    @property
    def merged_tasks(self) -> int:
        return self.task_outcomes.get("merged", 0)

    @property
    def settled_tasks(self) -> int:
        """Tasks whose fate is KNOWN (merged or closed unmerged)."""
        return (self.task_outcomes.get("merged", 0)
                + self.task_outcomes.get("closed_unmerged", 0))

    @property
    def unknown_tasks(self) -> int:
        return self.task_outcomes.get("unknown", 0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days,
            "delivered_tasks": self.delivered_tasks,
            "tasks_with_recorded_pr": self.tasks_with_recorded_pr,
            "tasks_without_recorded_pr": self.tasks_without_recorded_pr,
            "task_outcomes": dict(self.task_outcomes),
            "pr_outcomes": dict(self.pr_outcomes),
            "total_prs": self.total_prs,
            "merged_tasks": self.merged_tasks,
            "settled_tasks": self.settled_tasks,
            "unknown_tasks": self.unknown_tasks,
        }


async def compute_pr_outcome_metrics(
    store: Any, *, days: int | None = None,
) -> PrOutcomeReport:
    """What happened to the PRs of every DELIVERED task. Read-only.

    Scoped to tasks in `_PR_REACHED_STATUSES` on purpose: the question is "of
    the things we called successes, how many actually landed", so the
    population has to be exactly the set the old metric called successes.
    """
    from ..vcs.pr_outcome import UNKNOWN, counts, rollup

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)) if days else None
    tasks = [t for t in await store.list_tasks() if _within_window(t, cutoff)]
    delivered = [t for t in tasks if t.status in _PR_REACHED_STATUSES]

    rep = PrOutcomeReport(window_days=days, delivered_tasks=len(delivered))
    per_task: list[str] = []
    all_rows: list[str] = []
    for t in delivered:
        try:
            rows = await store.list_pr_outcomes(task_id=t.id)
        except Exception:  # noqa: BLE001 — telemetry must never crash a report
            rows = []
        if not rows:
            rep.tasks_without_recorded_pr += 1
            continue
        rep.tasks_with_recorded_pr += 1
        outs = [str(r.get("outcome") or UNKNOWN) for r in rows]
        all_rows.extend(outs)
        per_task.append(rollup(outs))

    # `counts` fills every key in `pr_outcome.OUTCOMES`, zeros included, so the
    # renderer never has to guess whether a missing key means zero or means the
    # bucket does not exist — and the vocabulary stays owned by one module.
    rep.task_outcomes = counts(per_task)
    rep.pr_outcomes = counts(all_rows)
    rep.total_prs = len(all_rows)
    return rep


def render_pr_outcome_lines(rep: PrOutcomeReport) -> list[str]:
    """The PR-outcome block, as plain lines. THE ONE RENDERER.

    `nh bench report` and `nh pr-outcomes show` both call this, so the caveats
    cannot be present on one surface and dropped from the other — which is the
    normal way an honest number becomes a dishonest one.

    Every figure is printed as `n/d` with `d` named. No blended percentage is
    produced anywhere, and when a denominator is zero the line says the figure
    is NOT YET MEASURABLE rather than printing 0%, because "we measured none of
    these" and "none of these landed" are opposite facts.
    """
    from ..vcs.pr_outcome import (CLOSED_UNMERGED, MERGED, MERGED_CAVEAT, OPEN,
                                  UNKNOWN, UNKNOWN_CAVEAT)

    t = rep.task_outcomes
    lines = [
        "PR OUTCOME — what became of the work, per delivered task",
        "",
        f"  delivered (reached AWAITING_APPROVAL/DONE)   {rep.delivered_tasks}",
        f"    ├─ with a recorded PR                      {rep.tasks_with_recorded_pr}",
        f"    └─ with no recorded PR                     {rep.tasks_without_recorded_pr}"
        "   (no PR opened, or predates this telemetry — not separable)",
        "",
        f"  of the {rep.tasks_with_recorded_pr} with a recorded PR:",
        f"    merged                                     {t.get(MERGED, 0)}",
        f"    closed unmerged                            {t.get(CLOSED_UNMERGED, 0)}",
        f"    still open                                 {t.get(OPEN, 0)}",
        f"    unknown (not measured)                     {t.get(UNKNOWN, 0)}",
        "",
    ]

    # Two denominators, both named, neither collapsed into "the" success rate.
    #
    # BOTH are gated on `settled_tasks`, not just the second one. When nothing
    # has a known fate the numerator is necessarily zero — for want of a
    # MEASUREMENT, not for want of merges — so `merged/delivered` would print a
    # confident "0%" that means the opposite of what it reads like. That is the
    # day-one state of this table on any existing database, i.e. the state a
    # reader is most likely to meet it in, and printing 0% there would rebuild
    # the very defect this module exists to correct. A percentage appears only
    # once at least one PR's fate is actually known.
    if not rep.settled_tasks:
        lines += [
            f"  merged / delivered   NOT YET MEASURABLE"
            f"  ({rep.merged_tasks}/{rep.delivered_tasks} measured)",
            "  merged / settled     NOT YET MEASURABLE — 0 PRs have a known "
            "fate. This is NOT a 0% merge rate; it is an unmeasured one. Run "
            "`nh pr-outcomes refresh` to resolve what can be resolved.",
        ]
    else:
        lines.append(
            f"  merged / delivered   {rep.merged_tasks}/{rep.delivered_tasks}"
            f"  = {rep.merged_tasks / rep.delivered_tasks:.0%}"
            "   (conservative: unknowns and un-opened PRs count AGAINST)")
        lines.append(
            f"  merged / settled     {rep.merged_tasks}/{rep.settled_tasks}"
            f"  = {rep.merged_tasks / rep.settled_tasks:.0%}"
            "   (only PRs whose fate is known; read it beside the unknown count)")
    if rep.total_prs != rep.tasks_with_recorded_pr:
        lines.append(
            f"  ({rep.total_prs} PRs across {rep.tasks_with_recorded_pr} tasks — "
            "multi-repo tasks roll up conservatively: any unmeasured PR makes "
            "the whole task unmeasured.)")

    lines += ["", "  " + MERGED_CAVEAT, "", "  " + UNKNOWN_CAVEAT]
    return lines


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
