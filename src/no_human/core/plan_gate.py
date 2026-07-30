"""The optional human plan-approval gate (GAP 1).

Per-task opt-in, default OFF: with ``task.config["plan_approval"]`` truthy the
task stops after the plan is generated and before the first implementation
session, so a human reads the plan the agent is about to spend tokens on.

Nothing new is invented for the parking itself. The gate raises an ordinary
``AMBIGUITY`` blocker, which ``blockers.triage`` already routes to
``AWAITING_INPUT`` — the state the board's "Needs Answer" lane, the drawer's
decision panel, the wake watcher and both reply paths (``nh reply`` and
``POST /api/tasks/{id}/reply``) already know how to render and clear.

This module is pure: no I/O, no DB, no git. The orchestrator and the two reply
paths call it and own their own persistence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..blockers import Blocker, BlockerCategory, BlockerOption
from .task import Task, TaskStatus

# The per-task option key. It lives on ``task.config`` because that is where
# every other per-task, human-only override already lives (`nh task config`,
# `pr_labels`, `backend`, the budget caps) — a run reads it, the agent never
# writes it.
CONFIG_KEY = "plan_approval"

# The gate's own state, on ``task.context`` beside the plan it is gating.
CONTEXT_KEY = "plan_approval"

STATE_AWAITING = "awaiting"
STATE_CORRECTING = "correcting"
STATE_APPROVED = "approved"

# A correction re-plans ONCE. Beyond that the gate parks without spending
# another planner session — a plan the human keeps rejecting is a decision to
# make, not a loop to run (CLAUDE.md #5).
MAX_REPLANS = 1

APPROVE_LABEL = "Approve the plan - start implementing"
_NO_PLAN = (
    "No plan was produced (planning is disabled for this run, or the planner "
    "assessed the task as a one-line change). Approve to implement without a "
    "plan, or reply with the approach you want."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def required(task: Task) -> bool:
    """True when this task opted into the gate."""
    return bool((task.config or {}).get(CONFIG_KEY))


def state(task: Task) -> dict[str, Any]:
    value = (task.context or {}).get(CONTEXT_KEY)
    return value if isinstance(value, dict) else {}


def awaiting(task: Task) -> bool:
    """True while a human still owes this task an approve-or-correct."""
    return state(task).get("state") in (STATE_AWAITING, STATE_CORRECTING)


def approved(task: Task) -> bool:
    return state(task).get("state") == STATE_APPROVED


def correction(task: Task) -> str:
    return str(state(task).get("correction") or "")


def replans_used(task: Task) -> int:
    try:
        return int(state(task).get("replans") or 0)
    except (TypeError, ValueError):
        return 0


def can_replan(task: Task) -> bool:
    return replans_used(task) < MAX_REPLANS


def pending_correction(task: Task) -> bool:
    """True when a correction reply is waiting to be acted on."""
    return state(task).get("state") == STATE_CORRECTING and bool(correction(task))


def park_patch(task: Task, plan_text: str) -> dict[str, Any]:
    """The ``plan_approval`` context value written when the gate parks.

    RFC 7396 shape (``Store.merge_context``): ``correction: None`` deletes a
    correction that has now been folded into the plan being shown.
    """
    return {
        "state": STATE_AWAITING,
        "plan": plan_text,
        "asked_at": _now(),
        "correction": None,
        "replans": replans_used(task),
    }


def reply_patch(task: Task, *, approve: bool, answer: str) -> dict[str, Any]:
    """The ``plan_approval`` context value written by a reply at the gate."""
    if approve:
        return {"state": STATE_APPROVED, "approved_at": _now(), "correction": None}
    return {"state": STATE_CORRECTING, "correction": answer, "corrected_at": _now()}


def replan_patch(task: Task) -> dict[str, Any]:
    """Consume one re-plan allowance."""
    return {"replans": replans_used(task) + 1}


def resume_status(task: Task, *, approve: bool) -> TaskStatus:
    """Where a reply resumes this task to.

    IMPLEMENTING for every reply the product has ever handled; PLANNING only
    for a correction at an un-approved plan gate, which must re-plan before it
    is allowed to spend an implementation session.
    """
    if awaiting(task) and not approve:
        return TaskStatus.PLANNING
    return TaskStatus.IMPLEMENTING


def build_blocker(task: Task, plan_text: str, *, capped: bool = False) -> Blocker:
    """The parked-for-approval blocker. AMBIGUITY so the existing taxonomy
    routes it to AWAITING_INPUT and notifies the human it is their move."""
    if capped:
        question = (
            "This plan has already been revised once with your correction. "
            "Approve it to start implementing, or stop the task - no further "
            "re-plan will run."
        )
    elif plan_text:
        question = (
            "Approve this plan before any implementation token is spent? "
            "Reply with a correction instead and it will be re-planned once."
        )
    else:
        question = _NO_PLAN
    return Blocker(
        category=BlockerCategory.AMBIGUITY,
        transient=False,
        confidence=1.0,
        goal=task.title,
        root_cause_hypothesis=(
            "Plan approval is enabled for this task, so implementation waits "
            "on a human reading the plan."
        ),
        evidence=plan_text or "(no plan produced)",
        question=question,
        options=[BlockerOption(label=APPROVE_LABEL, action={"approve_plan": True})],
    )


def build_correction_block(task: Task) -> str:
    """Planner-prompt block carrying the rejected plan and the human's
    correction. Empty string when there is no correction — a first plan's
    prompt stays byte-for-byte what it is today."""
    note = correction(task)
    if not note:
        return ""
    previous = str(state(task).get("plan") or "").strip()
    flat = note.strip().replace("\r", "")
    parts = [
        "PLAN CORRECTION - a human reviewed your previous plan and rejected "
        "it. Their correction is binding: the new plan must follow it, not "
        "argue with it. Do not widen the scope beyond what they asked for.\n",
        f"Their correction:\n  {flat}\n",
    ]
    if previous:
        parts.append("The plan they rejected:\n"
                     + "\n".join(f"  {ln}" for ln in previous.splitlines()) + "\n")
    return "\n".join(parts) + "\n"
