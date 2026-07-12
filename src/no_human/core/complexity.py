"""One explicit complexity tier per task (C1.5).

The pipeline had three DISJOINT complexity gates — the MoA fan-out (signal
count), extended thinking (files/plan-size/decompose), and the turn budget
(rides on thinking) — so a task could get 3 Opus proposers but no thinking,
and nothing recorded WHICH resourcing a task received. This module computes
one tier, stores it on ``task.context["complexity_tier"]``, and every knob
reads it. Boundaries deliberately mirror the pre-existing gates: the tier
makes resourcing VISIBLE and MEASURABLE first; retuning knobs per tier comes
later, with per-tier cost/quality data from /api/metrics.

Tiers only ever escalate (``upgrade_tier``): misclassifying complex→simple
under-resources and fails the task, so uncertainty errs toward MORE
resources, and later evidence (spec files, plan size) can raise but never
lower the tier.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .task import Task

TIERS = ("trivial", "simple", "standard", "complex")


def _rank(tier: str) -> int:
    return TIERS.index(tier) if tier in TIERS else 1


def compute_tier(task: "Task", moa_cfg: dict | None = None) -> tuple[str, list[str]]:
    """(tier, fired signal names) from signals knowable at intake.

    Signal set = the MoA gate's (multi-repo, many-criteria on OPERATOR-stated
    criteria, long-spec, ambiguous-spec) plus the thinking gate's post-spec
    signals when already present (files_to_change > 4, plan_size_warning).
    ≥2 signals or a decompose verdict ⇒ complex (the MoA bar); 1 ⇒ standard;
    0 ⇒ simple, or trivial when the spec is demonstrably tiny.
    """
    from .orchestrator import _moa_complexity_signals

    signals = list(_moa_complexity_signals(task, moa_cfg or {}))
    ctx = task.context or {}
    if len((ctx.get("spec") or {}).get("files_to_change") or []) > 4:
        signals.append("many-files")
    if ctx.get("plan_size_warning"):
        signals.append("large-plan")
    verdict = (ctx.get("eval_result") or {}).get("verdict")

    # The complex bar tracks the operator's min_signals when RAISED above
    # the default (an explicit cost knob); 0/1 stay fan-out overrides handled
    # at the MoA gate itself — a bar of 0 would tag every task complex and
    # turn thinking on for everything.
    bar = max(2, int((moa_cfg or {}).get("min_signals", 2)))
    if len(signals) >= bar or verdict == "decompose":
        return "complex", signals
    if len(signals) >= 1:
        return "standard", signals
    stated = ctx.get("original_criteria")
    if stated is None:
        stated = task.acceptance_criteria or []
    if len(stated) <= 2 and len(task.description or "") < 500 and not task.linked_repos:
        return "trivial", signals
    return "simple", signals


def store_tier(task: "Task", tier: str, signals: list[str]) -> bool:
    """Record the tier on task.context, escalate-only. Returns True when the
    stored tier changed (caller persists + emits)."""
    ctx = task.context or {}
    current = ctx.get("complexity_tier")
    if current and _rank(tier) <= _rank(current):
        return False
    ctx["complexity_tier"] = tier
    ctx["complexity_signals"] = signals
    task.context = ctx
    return True


def is_complex(task: "Task") -> bool:
    return ((task.context or {}).get("complexity_tier")) == "complex"
