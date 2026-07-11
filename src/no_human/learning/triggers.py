"""Knowledge triggers (W3.4, Devin-inspired): a confirmed memory/rule is
injected into a task's prompt only when it is RELEVANT, so context spend
(and noise) happens on-demand instead of always.

A memory's ``tags`` are its trigger condition: it is injected only when one
of its tags appears in the task's text (title + description + acceptance
criteria + changed files). A memory with NO tags is unconditional — always
injected, exactly as before (backward compatible). Pure functions so the
behaviour is unit-pinned; the orchestrator just filters and emits an audit
event naming what was injected vs suppressed.
"""

from __future__ import annotations

import json
from typing import Any


def _tags_of(memory: dict[str, Any]) -> list[str]:
    raw = memory.get("tags")
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(t) for t in raw]
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [str(t) for t in parsed] if isinstance(parsed, list) else []


def memory_is_triggered(memory: dict[str, Any], haystack: str) -> bool:
    """True if this memory should be injected for a task whose text is
    *haystack*. No tags → always (unconditional). Tags → only when one
    appears (case-insensitive substring) in the task text."""
    tags = [t for t in _tags_of(memory) if t.strip()]
    if not tags:
        return True  # no usable tags → unconditional (always inject)
    low = haystack.lower()
    return any(t.lower() in low for t in tags)


def filter_triggered(
    memories: list[dict[str, Any]], haystack: str,
) -> list[dict[str, Any]]:
    """The subset of *memories* whose trigger fires for this task."""
    return [m for m in memories if memory_is_triggered(m, haystack)]
