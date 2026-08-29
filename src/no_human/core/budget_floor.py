"""Warn a human, before a reject/reply dispatches, that a task is nearly out
of lifetime budget.

Two real incidents (2026-08-24) motivate this: e79db976 was rejected at 113%
of its lifetime cap (already over) and went straight to FAILED; c9f04943 was
rejected at 90.3% and would have died mid-rework on BUDGET_EXHAUSTED, losing
a HIGH-severity security fix's feedback. `nh reject` / `nh reply` (and their
board/API twins, send-back and reply) dispatch a task without ever looking at
remaining budget — the enforcement gate (`Orchestrator._check_lifetime_budget`
and friends) catches the overrun, but only AFTER the human's feedback is
already spent on an attempt that cannot finish.

This module adds no enforcement of its own — it is a pure, read-only,
fail-open advisory: compute the same cost-weighted spend the enforcement gate
would compute, and hand back one warning (text + structured dict) when the
task's remaining spend is below a floor fraction of its lifetime cap, so the
human can choose to refile
smaller or raise the cap BEFORE burning the rest of the budget on a dispatch
that is likely to die mid-attempt. Nothing here blocks, delays or alters the
action itself.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .pricing import weighted_tokens

if TYPE_CHECKING:
    from .bounds import Bounds
    from .db import Store
    from .task import Task

log = logging.getLogger(__name__)

# Fixed fraction of `lifetime_tokens` remaining below which reject/reply warn.
# Resolved at intake as a fixed floor (not a dynamic median-attempt-cost
# figure) — simple, predictable, and matches the acceptance criteria's own
# worked example (150 of 1000).
FLOOR_FRACTION = 0.15


@dataclass(frozen=True)
class BudgetFloorWarning:
    """What to tell a human about to reject/reply into a low-budget task."""

    task_id: str
    used_attempts: int
    cap_attempts: int
    remaining_attempts: int
    used_tokens: int
    cap_tokens: int
    remaining_tokens: int
    floor_tokens: int
    raise_to: dict[str, int]

    def message(self) -> str:
        """One human-readable line, byte-identical across CLI/API/board."""
        short = self.task_id[:8]
        pct = (self.remaining_tokens / self.cap_tokens * 100) if self.cap_tokens else 0.0
        return (
            f"budget floor: {short} has {self.remaining_tokens:,} of "
            f"{self.cap_tokens:,} cost-weighted tokens left "
            f"({pct:.1f}%, floor {FLOOR_FRACTION * 100:.0f}%) and "
            f"{self.remaining_attempts} of {self.cap_attempts} attempts left. "
            "This will likely die mid-attempt on BUDGET_EXHAUSTED and the "
            "feedback will be lost. Either refile this ticket smaller and "
            "inline-complete, or raise the cap deliberately: `nh task config "
            f"{short} lifetime_tokens={self.raise_to['lifetime_tokens']:,} "
            f"lifetime_attempts={self.raise_to['lifetime_attempts']}`. "
            "(Proceeding anyway — nothing was auto-raised.)"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "message": self.message(),
            "remaining_tokens": self.remaining_tokens,
            "cap_tokens": self.cap_tokens,
            "remaining_attempts": self.remaining_attempts,
            "cap_attempts": self.cap_attempts,
            "floor_tokens": self.floor_tokens,
            "raise_to": dict(self.raise_to),
        }


async def check_budget_floor(
    store: "Store", task: "Task", *, bounds: "Bounds"
) -> "BudgetFloorWarning | None":
    """`None` when the task is above the floor, or on any read failure.

    Fail-open: this must never be the reason a reject/reply is blocked or
    delayed, so any exception while reading spend/caps is logged at debug and
    swallowed — the action proceeds exactly as it would with no warning at
    all (same discipline as `Orchestrator._orphaned_ledger_residual`).

    Reuses, rather than re-derives, the two pieces of the enforcement gate
    that must never disagree with this warning: `Store.lifetime_usage_by_class`
    for what counts as spend (excludes infra/mechanical/dead-interrupted
    rows on both axes) and `Orchestrator._stored_token_cap` for the token cap
    (carries the raw-to-weighted cutover guard — re-deriving it would risk
    handing the human a 5x-wrong figure). `Orchestrator` is imported lazily to
    keep this module import-cheap and avoid any import cycle.
    """
    try:
        from .orchestrator import Orchestrator

        tcfg = task.config or {}
        used_attempts, included, _excluded = await store.lifetime_usage_by_class(task.id)
        used_tokens = weighted_tokens(**included)
        cap_tokens = Orchestrator._stored_token_cap(
            tcfg, "lifetime_tokens", bounds.lifetime_tokens, task)

        # Same tiny rule `Orchestrator._lifetime_limits` applies to
        # `lifetime_attempts` — a plain count, no unit, so no conversion.
        try:
            cap_attempts = int(tcfg.get("lifetime_attempts", bounds.lifetime_attempts))
        except (TypeError, ValueError):
            cap_attempts = bounds.lifetime_attempts
        if cap_attempts <= 0:
            cap_attempts = bounds.lifetime_attempts

        remaining_tokens = max(0, cap_tokens - used_tokens)
        remaining_attempts = max(0, cap_attempts - used_attempts)
        floor_tokens = int(cap_tokens * FLOOR_FRACTION)

        if remaining_tokens >= floor_tokens:
            return None

        # Same proportional-raise arithmetic as
        # `Orchestrator._budget_exhausted_blocker`, so the number offered
        # here is the number the blocker would offer if this dispatch dies.
        raise_tokens = math.ceil(used_tokens * 1.5 / 100_000) * 100_000
        raise_to = {
            "lifetime_tokens": raise_tokens,
            "lifetime_attempts": used_attempts + bounds.max_attempts,
        }
        return BudgetFloorWarning(
            task_id=task.id,
            used_attempts=used_attempts, cap_attempts=cap_attempts,
            remaining_attempts=remaining_attempts,
            used_tokens=used_tokens, cap_tokens=cap_tokens,
            remaining_tokens=remaining_tokens, floor_tokens=floor_tokens,
            raise_to=raise_to,
        )
    except Exception:
        log.debug("check_budget_floor: usage/cap read failed, skipping warning",
                   exc_info=True)
        return None
