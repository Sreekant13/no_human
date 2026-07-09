"""Applying a blocker option's action (PLAN.md Part 22).

A `BlockerOption` may carry an action that turns its label into something the
product can honour — "raise the limit for this task" actually raising the limit.

Two rules hold this together:

  - **Only a human applies an action.** `nh reply --choose` and the board's reply
    endpoint are the sole callers. The agent never reaches this module, and
    `parse_blocker` strips actions off anything the agent emits. Letting the
    agent set `task.config` would let it resolve a blocker by weakening the very
    gate that raised it.
  - **One verb, whitelisted keys.** `set_task_config` may write exactly the two
    size limits `_over_size_limits` reads. This is not an action engine; adding a
    verb should take a deliberate change here, not a new key in a JSON blob.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only, avoids an import cycle
    from ..core.task import Task

SET_TASK_CONFIG = "set_task_config"

# Exactly the keys `Orchestrator._size_limits` honours as a per-task override.
ALLOWED_TASK_CONFIG_KEYS = frozenset({"max_lines_changed", "max_files_changed"})


class ActionError(ValueError):
    """The action is malformed or not permitted. Surfaced to the human who chose
    it, never swallowed — a silently ignored action is a button that lies."""


def apply_action(task: "Task", action: dict[str, Any] | None) -> str | None:
    """Apply `action` to `task` in place. Returns a human-readable summary of
    what changed, or None when the option carried no action."""
    if not action:
        return None
    if not isinstance(action, dict):
        raise ActionError(f"action must be an object, got {type(action).__name__}")

    unknown = set(action) - {SET_TASK_CONFIG}
    if unknown:
        raise ActionError(f"unknown action verb(s): {', '.join(sorted(unknown))}")

    settings = action.get(SET_TASK_CONFIG)
    if not isinstance(settings, dict) or not settings:
        raise ActionError(f"{SET_TASK_CONFIG} needs a non-empty object of settings")

    resolved: dict[str, int] = {}
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
        resolved[key] = value

    task.config = {**(task.config or {}), **resolved}
    return ", ".join(f"{k}={v}" for k, v in sorted(resolved.items()))
