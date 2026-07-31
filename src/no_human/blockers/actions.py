"""Applying a blocker option's action (PLAN.md Part 22).

A `BlockerOption` may carry an action that turns its label into something the
product can honour — "raise the limit for this task" actually raising the limit.

Two rules hold this together:

  - **Only a human applies an action.** `nh reply --choose` and the board's reply
    endpoint are the sole callers. The agent never reaches this module, and
    `parse_blocker` strips actions off anything the agent emits. Letting the
    agent set `task.config` would let it resolve a blocker by weakening the very
    gate that raised it.
  - **One verb, whitelisted keys.** `set_task_config` may write exactly the
    size limits and lifetime caps the orchestrator reads as per-task
    overrides. This is not an action engine; adding a verb should take a
    deliberate change here, not a new key in a JSON blob.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

# `core.pricing` is a leaf (it imports nothing from this package), so this
# cannot reintroduce the cycle the TYPE_CHECKING guard below exists for.
from ..core.pricing import (
    BUDGET_UNIT_KEY, TOKEN_CAP_KEYS, WEIGHTED_UNIT, config_is_weighted,
    raw_cap_as_weighted,
)

if TYPE_CHECKING:  # pragma: no cover — typing only, avoids an import cycle
    from ..core.task import Task

SET_TASK_CONFIG = "set_task_config"
# Terminal outcome (SCRUM-22): the option's meaning is "stop — keep the work
# parked as-is". Both reply paths check ``is_terminal_action`` and KEEP the
# task in its parked state instead of resuming; the live bug was the stop
# option carrying no action, which made "answer + resume" silently invert the
# human's explicit stop. Exactly ``{"park": true}``, alone — parse_blocker
# strips agent options to labels, so the agent can never emit it.
PARK = "park"
# Plan-approval gate (GAP 1): the option's meaning is "the plan is good — start
# implementing". Structural, not a string match on the answer text, so the
# board's one-click option and `nh reply --choose` mean the same thing and a
# free-text reply is unambiguously a correction. Exactly
# ``{"approve_plan": true}``, alone — parse_blocker strips agent options to
# labels, so the agent can never approve its own plan.
APPROVE_PLAN = "approve_plan"

# Exactly the per-task overrides the orchestrator honours: the two size
# limits (`_size_limits`), the two lifetime caps (`_lifetime_limits`), and the
# per-attempt token cap (`_attempt_token_cap`).
ALLOWED_TASK_CONFIG_KEYS = frozenset({
    "max_lines_changed", "max_files_changed",
    "lifetime_attempts", "lifetime_tokens",
    "attempt_tokens",
})


class ActionError(ValueError):
    """The action is malformed or not permitted. Surfaced to the human who chose
    it, never swallowed — a silently ignored action is a button that lies."""


def is_terminal_action(action: dict[str, Any] | None) -> bool:
    """True when picking this option means the task STAYS parked (no resume)."""
    return isinstance(action, dict) and action.get(PARK) is True


def is_plan_approval_action(action: dict[str, Any] | None) -> bool:
    """True when picking this option APPROVES the parked plan (GAP 1)."""
    return isinstance(action, dict) and action.get(APPROVE_PLAN) is True


def apply_action(
    task: "Task", action: dict[str, Any] | None, *, human_override: bool = False
) -> str | None:
    """Apply `action` to `task` in place. Returns a human-readable summary of
    what changed, or None when the option carried no action.

    `human_override=False` (the default — every blocker-option path) keeps
    the never-lower guard: a "raise the limit" option must never lower an
    already bigger stored cap. `human_override=True` is passed ONLY by the
    human CLI (`nh task config KEY=VALUE`), which sets the exact value —
    raising or lowering — because the human is the gate.
    """
    if not action:
        return None
    if not isinstance(action, dict):
        raise ActionError(f"action must be an object, got {type(action).__name__}")

    unknown = set(action) - {SET_TASK_CONFIG, PARK, APPROVE_PLAN}
    if unknown:
        raise ActionError(f"unknown action verb(s): {', '.join(sorted(unknown))}")

    if APPROVE_PLAN in action:
        if action.get(APPROVE_PLAN) is not True or len(action) != 1:
            raise ActionError(
                'a plan-approval action must be exactly {"approve_plan": true}, alone')
        return "plan approved - implementation may start"

    if PARK in action:
        if action.get(PARK) is not True or len(action) != 1:
            raise ActionError(
                'a park action must be exactly {"park": true}, alone')
        return "parked — work kept as-is"

    settings = action.get(SET_TASK_CONFIG)
    if not isinstance(settings, dict) or not settings:
        raise ActionError(f"{SET_TASK_CONFIG} needs a non-empty object of settings")

    existing = task.config or {}
    resolved: dict[str, int] = {}
    notes: dict[str, str] = {}
    for key, raw in settings.items():
        if key not in ALLOWED_TASK_CONFIG_KEYS:
            raise ActionError(
                f"{key!r} is not settable per task "
                f"(allowed: {', '.join(sorted(ALLOWED_TASK_CONFIG_KEYS))})"
            )
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ActionError(f"{key} must be an integer, got {raw!r}") from None
        if value <= 0:
            raise ActionError(f"{key} must be positive, got {value}")
        # Every allowed key is a resource cap: a "raise the limit" action must
        # never lower one that is already higher (e.g. BUDGET_EXHAUSTED's
        # raise option overwriting a bigger stored cap with its fixed value).
        # The human CLI path (human_override=True) is the explicit gate and sets
        # the exact requested value instead, raising or lowering.
        prior = existing.get(key)
        if isinstance(prior, int) and not isinstance(prior, bool):
            # ...COMPARED IN ONE UNIT. The token caps became cost-weighted on
            # 2026-07-31 and 165 pre-cutover overrides on this install are raw
            # numbers 5x too large for the new scale. Comparing a weighted
            # request against a raw prior means the raw one always "wins" the
            # never-lower guard and is then written back as if it were
            # weighted — so the stale 12M-68M ceilings would be permanent,
            # unreachable by the very flow that exists to correct a budget.
            # Normalising the prior first is what makes them correctable.
            if key in TOKEN_CAP_KEYS and not config_is_weighted(existing):
                prior = max(raw_cap_as_weighted(prior), 1)
        if (not human_override
                and isinstance(prior, int) and not isinstance(prior, bool)
                and prior > value):
            resolved[key] = prior
            notes[key] = f"{key}={prior} (kept; requested {value} would lower it)"
        else:
            resolved[key] = value
            notes[key] = f"{key}={value}"

    task.config = {**existing, **resolved}
    # Stamp the unit on any write that touches a token cap, so this config is
    # never re-converted. Written here rather than asked of every caller: the
    # blocker's raise option, `nh task config` and the API all land in this one
    # function, and a caller that forgot would silently shrink its own cap by
    # 5x on the next read.
    if TOKEN_CAP_KEYS & set(resolved):
        # The marker describes the WHOLE dict, so stamping it makes a claim
        # about every token cap in it — including ones this write never
        # touched. A partial write is the NORMAL case, not a corner: the
        # BUDGET_EXHAUSTED raise option writes {lifetime_attempts,
        # lifetime_tokens} and never attempt_tokens, and 162 tasks on this
        # install carry both keys in raw units. Stamping without converting
        # the untouched sibling promoted a raw 6,000,000 attempt_tokens to
        # 6,000,000 weighted — 5.04x — and permanently, since the marker is
        # then present. The damage is not mainly dollars: on all 27 escalated
        # tasks that reach a raise, the inflated attempt cap lands at or above
        # the remaining lifetime budget, so the per-attempt brake goes inert
        # and the bounded loop degenerates to a single attempt — verbatim the
        # v6 taxonomy failure that `Bounds.attempt_tokens` exists to prevent.
        #
        # So: bring every untouched cap into the unit the marker is about to
        # claim, using the same conversion the reader would have applied. The
        # invariant this restores is that a write to one key cannot change
        # what another key MEANS.
        if not config_is_weighted(existing):
            for other in TOKEN_CAP_KEYS - set(resolved):
                stale = existing.get(other)
                if (isinstance(stale, int) and not isinstance(stale, bool)
                        and stale > 0):
                    task.config[other] = max(raw_cap_as_weighted(stale), 1)
        task.config[BUDGET_UNIT_KEY] = WEIGHTED_UNIT
    return ", ".join(notes[k] for k in sorted(notes))
