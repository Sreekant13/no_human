"""Knowledge triggers (W3.4): a confirmed memory/rule is
injected into a task's prompt only when it is RELEVANT, so context spend
(and noise) happens on-demand instead of always.

A memory's ``tags`` are its trigger condition: it is injected only when one
of its tags appears in the task's text (title + description + acceptance
criteria + changed files). The task text — the "haystack" every function
below matches against — has exactly one producer,
``Orchestrator._trigger_haystack`` (``core/orchestrator.py``); "changed
files" there means the plan's ``FILES TO CHANGE/CREATE`` paths, or the
ticket-named paths before a plan exists. A memory with NO tags is
unconditional — always injected, exactly as before (backward compatible).
Pure functions so the behaviour is unit-pinned; the orchestrator just
filters and emits an audit event naming what was injected vs suppressed.
"""

from __future__ import annotations

import json
import re
from typing import Any


def _term_fires(term: str, haystack_lower: str) -> bool:
    """True when *term* appears in *haystack_lower* as a whole word/phrase —
    NOT merely as a substring.

    2026-09-01 effectiveness study: a bare ``term in haystack`` check let a
    tag as short as ``fact`` fire inside ``artefact``, and inside real task
    text (an AC containing "suffix rule") it injected up to 25 irrelevant
    rules into one task. Boundaries are ASCII word-character transitions
    (alnum + underscore) at the very ends of *term*, checked with lookaround
    rather than ``\\b`` so a multi-word phrase (``"suffix rule"``) is anchored
    only at its two outer ends, not at the internal space. A term that is
    itself a path or filename (``triggers.py``) still matches literally
    against a path in the haystack: the "." inside it is never a boundary
    check, only the letters at the term's own start/end are.
    """
    term = (term or "").strip().lower()
    if not term:
        return False
    pattern = r"(?<![0-9a-z_])" + re.escape(term) + r"(?![0-9a-z_])"
    return re.search(pattern, haystack_lower) is not None


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
    appears (case-insensitive, WHOLE WORD/PHRASE — see ``_term_fires``) in
    the task text.

    A CANONICAL vocabulary tag (B3, ``learning/vocab.py``) triggers on its
    alias family (minus the deliberately generic ``NON_TRIGGER_ALIASES``),
    not just its own value — a lesson stored under ``environment`` must still
    fire for a task that says "venv". A PROVENANCE tag contributes no trigger
    terms at all, so a memory tagged only with provenance never auto-injects.
    A tag from outside the vocabulary (pre-B3 rows, outcome-path enum tags)
    matches on its literal value exactly as before — including a tag that is
    itself a path or filename (e.g. ``triggers.py``), which matches literally
    against the file signal in *haystack* and nowhere else."""
    from .vocab import trigger_terms
    tags = [t for t in _tags_of(memory) if t.strip()]
    if not tags:
        return True  # no usable tags → unconditional (always inject)
    low = haystack.lower()
    return any(_term_fires(term, low) for t in tags for term in trigger_terms(t))


def filter_triggered(
    memories: list[dict[str, Any]], haystack: str,
) -> list[dict[str, Any]]:
    """The subset of *memories* whose trigger fires for this task."""
    return [m for m in memories if memory_is_triggered(m, haystack)]


def matched_tags(memory: dict[str, Any], haystack: str) -> list[str]:
    """The STORED tags (not their expanded alias terms) of *memory* that
    caused it to fire for *haystack* — order-preserving, deduplicated.

    Written for the D3/2026-09-01 audit requirement: ``learning_events``
    must record WHICH tags fired for each injection, not merely THAT one
    did. An untagged (unconditional) memory returns ``[]`` — there is no
    tag to name, even though it always injects; a caller writing an audit
    row for it should say so itself rather than read an empty list as "no
    reason", which is a different, false claim for an untagged rule."""
    from .vocab import trigger_terms
    tags = [t for t in _tags_of(memory) if t.strip()]
    if not tags:
        return []
    low = haystack.lower()
    out: list[str] = []
    for t in tags:
        if t in out:
            continue
        if any(_term_fires(term, low) for term in trigger_terms(t)):
            out.append(t)
    return out


def trigger_reason(memory: dict[str, Any], haystack: str) -> dict[str, Any]:
    """The audit-ready shape for WHY *memory* fired for *haystack* — exactly
    the caller-side check `matched_tags`'s docstring asks for, so no caller
    (there is currently one: the `learning_events` 'inject' audit write in
    `Orchestrator._load_active_memories`) has to remember it itself and risk
    writing the false claim the docstring warns about.

    ``{"unconditional": True}`` when *memory* carries no usable tags (it
    always injects, so there is no tag to name as the reason); otherwise
    ``{"tags": [...]}`` — the STORED tags from `matched_tags` that actually
    fired. Never both, and never an empty ``"tags"`` list standing in for
    "unconditional"."""
    if not [t for t in _tags_of(memory) if t.strip()]:
        return {"unconditional": True}
    return {"tags": matched_tags(memory, haystack)}


def playbook_is_triggered(playbook: dict[str, Any], haystack: str) -> bool:
    """True if this playbook (1.4) applies to a task whose text is *haystack*.

    Unlike a memory (no tags → always inject), a playbook with NO trigger
    keywords NEVER auto-matches — a heavy, specific procedure must not attach
    itself to unrelated tasks. Keywords match case-insensitively, as whole
    words/phrases (see ``_term_fires``).
    """
    kws = [k for k in _json_list(playbook.get("trigger_keywords")) if k.strip()]
    if not kws:
        return False  # no trigger → manual-only, never auto-injected
    low = haystack.lower()
    return any(_term_fires(k, low) for k in kws)


def select_playbook(
    playbooks: list[dict[str, Any]], haystack: str,
) -> dict[str, Any] | None:
    """The single best-matching playbook for a task (the first that triggers).
    At most one is injected, to keep the coder prompt focused."""
    for pb in playbooks:
        if playbook_is_triggered(pb, haystack):
            return pb
    return None
