"""Knowledge triggers (W3.4): a confirmed memory/rule is
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


def _json_list(raw: Any) -> list[str]:
    """A stored JSON array (or a real list) → list[str]; anything else → []."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(t) for t in raw]
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [str(t) for t in parsed] if isinstance(parsed, list) else []


def _tags_of(memory: dict[str, Any]) -> list[str]:
    return _json_list(memory.get("tags"))


def memory_is_triggered(memory: dict[str, Any], haystack: str) -> bool:
    """True if this memory should be injected for a task whose text is
    *haystack*. No tags → always (unconditional). Tags → only when one
    appears (case-insensitive substring) in the task text.

    A CANONICAL vocabulary tag (B3, ``learning/vocab.py``) triggers on its
    alias family (minus the deliberately generic ``NON_TRIGGER_ALIASES``),
    not just its own value — a lesson stored under ``environment`` must still
    fire for a task that says "venv". A PROVENANCE tag contributes no trigger
    terms at all, so a memory tagged only with provenance never auto-injects.
    A tag from outside the vocabulary (pre-B3 rows, outcome-path enum tags)
    matches on its literal value exactly as before."""
    from .vocab import trigger_terms
    tags = [t for t in _tags_of(memory) if t.strip()]
    if not tags:
        return True  # no usable tags → unconditional (always inject)
    low = haystack.lower()
    return any(term in low for t in tags for term in trigger_terms(t))


def filter_triggered(
    memories: list[dict[str, Any]], haystack: str,
) -> list[dict[str, Any]]:
    """The subset of *memories* whose trigger fires for this task."""
    return [m for m in memories if memory_is_triggered(m, haystack)]


def playbook_is_triggered(playbook: dict[str, Any], haystack: str) -> bool:
    """True if this playbook (1.4) applies to a task whose text is *haystack*.

    Unlike a memory (no tags → always inject), a playbook with NO trigger
    keywords NEVER auto-matches — a heavy, specific procedure must not attach
    itself to unrelated tasks. Keywords match case-insensitively as substrings.
    """
    kws = [k for k in _json_list(playbook.get("trigger_keywords")) if k.strip()]
    if not kws:
        return False  # no trigger → manual-only, never auto-injected
    low = haystack.lower()
    return any(k.lower() in low for k in kws)


def select_playbook(
    playbooks: list[dict[str, Any]], haystack: str,
) -> dict[str, Any] | None:
    """The single best-matching playbook for a task (the first that triggers).
    At most one is injected, to keep the coder prompt focused."""
    for pb in playbooks:
        if playbook_is_triggered(pb, haystack):
            return pb
    return None
