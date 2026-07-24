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


def apply_action(task: "Task", action: dict[str, Any] | None) -> str | None:
    """Apply `action` to `task` in place. Returns a human-readable summary of
    what changed, or None when the option carried no action."""
    if not action:
        return None
    if not isinstance(action, dict):
        raise ActionError(f"action must be an object, got {type(action).__name__}")

    unknown = set(action) - {SET_TASK_CONFIG, PARK}
    if unknown:
        raise ActionError(f"unknown action verb(s): {', '.join(sorted(unknown))}")

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
        prior = existing.get(key)
        if isinstance(prior, int) and not isinstance(prior, bool) and prior > value:
            resolved[key] = prior
            notes[key] = f"{key}={prior} (kept; requested {value} would lower it)"
        else:
            resolved[key] = value
            notes[key] = f"{key}={value}"

    task.config = {**existing, **resolved}
    return ", ".join(notes[k] for k in sorted(notes))
