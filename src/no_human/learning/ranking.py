"""Memory lifecycle B: scored selection.

`triggers.filter_triggered` (the tag/substring pre-filter) and the vendor-term
screen decide WHICH memories are eligible for a task. This module decides the
ORDER they fill the existing char budgets in, once they are eligible — it
never widens or narrows the eligible set.

SCORE FORMULA
=============

    score = importance_weight(tier) * exp(-days_since_last_used / 14)
            * (1 + use_count) / (1 + max_use_count)

- ``importance_weight``: ``{"high": 1.0, "med": 0.5, "low": 0.2}``, keyed by
  the same ``importance:high`` / ``importance:low`` tags `prompt_blocks.py`
  already tiers on (``importance_tier`` below is the one place that reads
  them, so the two can't drift).
- Recency is exponential decay with a 14-day time constant: a memory used
  today scores 1.0 on this factor, a memory unused for 14 days scores
  ``e^-1 ≈ 0.368``, and one whose ``last_used_at`` is NULL/unparseable is
  treated as 30 days stale (``UNKNOWN_RECENCY_DAYS``) rather than either
  extreme (never decays a brand-new memory to zero, never favors it over a
  memory that has *proven* recent use).
- Frequency is `use_count` (part A's ledger column, absent from this repo
  today — read defensively with ``.get``) normalized against the corpus
  maximum, with **add-one smoothing on both sides**:
  ``(1 + use_count) / (1 + max_use_count)``. Two deliberate consequences:
  a never-used memory still scores a nonzero ``1 / (1 + max)`` rather than
  being annihilated, and when EVERY memory's ``use_count`` is null/0 (no
  ledger yet, or a fresh install) the factor is ``1 / 1 = 1.0`` for all of
  them — scoring collapses to ``importance x recency`` with no branch,
  which is exactly the empty-ledger fallback the spec asks for.

WORKED EXAMPLES (``max_use_count = 8`` across the batch)
----------------------------------------------------------

1. High-importance, used 7 days ago, ``use_count = 8``::

       1.0 * exp(-7/14) * (1+8)/(1+8) = 1.0 * 0.60653... * 1.0 = 0.60653...

2. Medium-importance, used today (0 days), ``use_count = 4``::

       0.5 * exp(0/14) * (1+4)/(1+8) = 0.5 * 1.0 * 0.55555... = 0.27777...

3. Low-importance, used 28 days ago, ``use_count = 0``::

       0.2 * exp(-28/14) * (1+0)/(1+8) = 0.2 * 0.13534... * 0.11111... = 0.00300...

Ordering: example 1 > example 2 > example 3, as the tiering intends.

SELECTION
=========

``rank_and_select`` takes the already trigger-filtered, already term-screened
candidate set (this module never re-derives eligibility) and returns it
reordered by score, subject to two constraints:

- **Ceiling** (``MEMORY_CEILING = 25``): a hard clip, never an exception.
  ``build_memories_block``'s char budgets (``_RULES_CRITICAL_CAP`` /
  ``_RULES_RELEVANT_CAP``) still apply on top of this — the ceiling bounds
  the candidate *count*, the char caps bound what actually fits.
- **Floor** (top ``FLOOR_RANK = 5`` importance:high memories BY USE_COUNT,
  ranked WITHIN the high tier): always selected, ahead of the score-ordered
  remainder, regardless of their own score. This is *within-tier* ranking on
  purpose — a handful of very-frequently-used medium/low rules must not push
  a rarely-scored-but-heavily-used high-importance rule out of its floor
  protection just because they happen to have bigger raw ``use_count``
  numbers; the floor is about the five best-used HIGH rules, not the five
  best-used rules overall. A high-importance memory with no recorded use
  (``use_count`` 0/None) is not floor-eligible — an empty ledger must not
  manufacture a floor set of five arbitrary rows (see "empty ledger" below).

Tie-break, everywhere: ascending ``id`` (the primary key). No timestamps, no
randomness — the same corpus + same ledger state always produces the same
order.

EMPTY LEDGER
============

When no memory in the candidate set has a positive ``use_count`` (part A not
yet populated, or a fresh install), the floor set is empty — there is no
frequency signal to rank a top-5 on — and every memory's frequency factor is
1.0, so ``score = importance_weight * recency`` for the whole batch. Ordering
degrades to importance x recency with no special-casing required.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from .triggers import _json_list

IMPORTANCE_WEIGHTS: dict[str, float] = {"high": 1.0, "med": 0.5, "low": 0.2}
RECENCY_TIME_CONSTANT_DAYS = 14.0
UNKNOWN_RECENCY_DAYS = 30.0  # last_used_at NULL/unparseable -> assume 30d stale

MEMORY_CEILING = 25
FLOOR_RANK = 5


def importance_tier(memory: dict[str, Any]) -> str:
    """``"high" | "med" | "low"`` from a memory's tags.

    The one place tiering is defined — ``prompt_blocks.build_memories_block``
    delegates here instead of re-reading tags itself. High wins over low if a
    row somehow carries both (matches the precedence the inline version in
    `prompt_blocks.py` already had). Tags are parsed via `triggers._json_list`
    so a JSON-STRING `tags` column (as stored) tiers correctly, not just a
    Python list already in memory.
    """
    tags = _json_list(memory.get("tags"))
    if "importance:high" in tags:
        return "high"
    if "importance:low" in tags:
        return "low"
    return "med"


def _id_key(memory: dict[str, Any]) -> str:
    """A total-order, always-comparable sort key for a memory's id."""
    mid = memory.get("id")
    return "" if mid is None else str(mid)


def _use_count(memory: dict[str, Any]) -> int:
    """``use_count`` defensively: part A's column may not exist at all, may be
    NULL, or (defensive only) may be a non-numeric value. Any of those -> 0.
    """
    raw = memory.get("use_count")
    if raw is None:
        return 0
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _days_since(memory: dict[str, Any], now: datetime) -> float:
    """Days between ``memory["last_used_at"]`` and *now*. NULL/unparseable ->
    ``UNKNOWN_RECENCY_DAYS``. Clock skew (a stamp in the future) clamps to 0
    rather than going negative and inflating the recency factor past 1.0.
    """
    raw = memory.get("last_used_at")
    if not raw:
        return UNKNOWN_RECENCY_DAYS
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return UNKNOWN_RECENCY_DAYS
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = (now - dt).total_seconds() / 86400.0
    return max(0.0, days)


def memory_score(
    memory: dict[str, Any], *, now: datetime, max_use_count: int,
) -> float:
    """``importance_weight * exp(-days/14) * (1+use_count)/(1+max_use_count)``.
    See the module docstring for the formula and worked examples.
    """
    weight = IMPORTANCE_WEIGHTS[importance_tier(memory)]
    recency = math.exp(-_days_since(memory, now) / RECENCY_TIME_CONSTANT_DAYS)
    frequency = (1 + _use_count(memory)) / (1 + max(0, max_use_count))
    return weight * recency * frequency


def rank_and_select(
    memories: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    ceiling: int = MEMORY_CEILING,
    floor_rank: int = FLOOR_RANK,
) -> list[dict[str, Any]]:
    """Score *memories* (already trigger-filtered + term-screened) and return
    them in selection order, clipped to *ceiling*.

    Pure and deterministic: no I/O, no randomness, no wall-clock read unless
    *now* is omitted (callers that care about determinism, including every
    test, pass it explicitly).
    """
    memories = list(memories or [])
    if now is None:
        now = datetime.now(timezone.utc)

    max_use_count = max((_use_count(m) for m in memories), default=0)

    # Floor: top `floor_rank` BY USE_COUNT, ranked WITHIN the high-importance
    # tier only (not the whole candidate set) — see module docstring. A row
    # with no recorded use is not floor-eligible.
    floor_candidates = [
        m for m in memories
        if importance_tier(m) == "high" and _use_count(m) > 0
    ]
    floor_candidates.sort(key=lambda m: (-_use_count(m), _id_key(m)))
    floor_ids = {_id_key(m) for m in floor_candidates[:floor_rank]}

    scored = [(m, memory_score(m, now=now, max_use_count=max_use_count))
              for m in memories]

    def sort_key(item: tuple[dict[str, Any], float]) -> tuple[int, float, str]:
        m, score = item
        is_floor = _id_key(m) in floor_ids
        return (0 if is_floor else 1, -score, _id_key(m))

    ordered = [m for m, _score in sorted(scored, key=sort_key)]
    return ordered[:ceiling]
