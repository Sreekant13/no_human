"""Answer memory for blocker questions (survives attempt death).

Live defect 2026-08-12/13: a task asked the identical AMBIGUITY question three
times because the operator's ``nh reply`` answer lived only in the attempt
that consumed it — when that attempt died (quota cut, edit-loop abort), the
next attempt re-derived the same ambiguity and re-asked, costing a human
round-trip each time.

The fix keeps every answer on the TASK record, not the attempt: an answer
already lives in ``tasks.context.human_replies`` (the CLI ``reply`` command
and the ``/api/tasks/{id}/reply`` endpoint both append there), so this module
is pure, I/O-free lookup and shaping over that same list — no new storage.

Matching is on a normalized-text hash only (no fuzzy/semantic matching, no
cross-task sharing): two questions that differ only in case, whitespace, or
trailing punctuation are "the same" question; anything else is not.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

_WHITESPACE_RE = re.compile(r"\s+")
_TRAILING_PUNCT_RE = re.compile(r"[?!.\s]+$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_question(text: str | None) -> str:
    """Lowercase, trim, collapse every run of internal whitespace to one
    space, and strip trailing ``?!.`` (and the whitespace around them).

    ``"What is the answer ?"`` and ``"what  is\\nthe answer"`` normalize to
    the same string; a genuinely different question does not.
    """
    if not text:
        return ""
    collapsed = _WHITESPACE_RE.sub(" ", text.strip().lower())
    return _TRAILING_PUNCT_RE.sub("", collapsed).strip()


def question_hash(text: str | None) -> str:
    """SHA-256 hex digest of the normalized question, or ``""`` for a blank
    one — a blank question never matches anything (there is no "same" empty
    question to reuse an answer for)."""
    normalized = normalize_question(text)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def answer_record(
    *, question: str | None, answer: str | None, attempt_id: str, source: str,
) -> dict[str, Any]:
    """The ``human_replies`` entry a human's answer to *question* produces.

    Keeps the pre-existing keys (``at``, ``question``, ``answer``,
    ``applied``) byte-for-byte — callers that don't know about reuse (the
    board, old rows) still read exactly what they always read — and adds the
    provenance this feature needs: ``question_hash``, ``answered_at`` (ISO
    UTC), ``source_attempt_id``, ``source``. ``applied`` defaults to ``None``;
    the reply handler overwrites it once it knows whether an action ran.
    """
    now = _now()
    return {
        "at": now,
        "question": question,
        "answer": answer,
        "applied": None,
        "question_hash": question_hash(question),
        "answered_at": now,
        "source_attempt_id": attempt_id or "",
        "source": source,
    }


def find_stored_answer(
    replies: list[Any] | None, question: str | None,
) -> dict[str, Any] | None:
    """The most recent stored human answer matching *question*'s normalized
    hash, or ``None``.

    Tolerates every shape already in the wild: bare strings (old rows /
    entries with no question of their own — never a match), and dicts with no
    ``question_hash`` (the entry's own ``question`` is hashed on the fly, so
    rows written before this feature still match). Skips:

    * entries with a blank ``answer`` — nothing to reuse;
    * entries whose ``applied`` is truthy — an option whose *action* already
      ran is a human-only privilege, never silently re-applied;
    * entries whose own ``source`` is ``"reuse"`` — a replay is never itself a
      source; matching always resolves back to the ORIGINAL human answer, so
      provenance (``source_attempt_id``) stays the attempt a human actually
      answered, not a previous reuse.
    """
    target = question_hash(question)
    if not target:
        return None
    best: dict[str, Any] | None = None
    for entry in replies or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("source") == "reuse":
            continue
        if not entry.get("answer"):
            continue
        if entry.get("applied"):
            continue
        entry_hash = entry.get("question_hash") or question_hash(entry.get("question"))
        if entry_hash == target:
            best = entry
    return best


def reuse_record(stored: dict[str, Any], *, reused_in_attempt_id: str) -> dict[str, Any]:
    """The ``human_replies`` entry appended when *stored* is replayed for a
    later attempt's identical question. Carries both attempt ids so the
    ``answer_reused`` event (and any future audit) can state provenance in
    full: which attempt originally got the human answer, and which attempt
    just reused it without asking again."""
    now = _now()
    return {
        "at": now,
        "question": stored.get("question"),
        "answer": stored.get("answer"),
        "applied": None,
        "question_hash": (
            stored.get("question_hash") or question_hash(stored.get("question"))
        ),
        "answered_at": now,
        "source": "reuse",
        "source_attempt_id": stored.get("source_attempt_id") or "",
        "reused_in_attempt_id": reused_in_attempt_id or "",
    }
