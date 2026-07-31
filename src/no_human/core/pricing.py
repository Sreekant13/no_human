"""Token-class price weights — the ONE Python price table.

A token is not a token. Anthropic bills three of the classes the ledger
records at three different rates, relative to one FRESH input token:

  * fresh in/out       1.0   (the ``tokens_used`` column: input + output,
                              summed by the backend before it ever reaches us)
  * cache CREATION     1.25  (a 5-minute cache write)
  * cache READ         0.1

Summing them 1:1 does not approximate cost, it approximates *conversation
length*. Task d6e4b72a was killed at "12,367,237/12,000,000 tokens" having
spent, in that attempt, 1,697 fresh + 173,190 cache-write + 6,589,429
cache-read — 877,127 fresh-equivalent tokens, roughly a fourteenth of the
number the gate printed. Measured over this install's whole ledger (193 tasks,
602 attempts, 1.718 BILLION raw tokens) the weighted total is 0.1985x the raw
total, so a raw-token cap bounds about five times less real spend than its
number suggests — and it bounds a DIFFERENT quantity for every task, because
the per-task weighted/raw ratio ranges from 0.122 to 0.697.

ONE table, deliberately. ``eval/northstar.py`` already carried these three
multipliers as inline literals for its ``cost_ratio``; it now imports them
from here, so the benchmark and the budget gate cannot drift apart. The
web UI's ``web/src/cost.js`` is the dollar-denominated twin of this file and
must stay in step — note that it currently prices cache CREATION at the fresh
rate (1.0, not 1.25), an under-count of the cheapest class documented there
and not silently "fixed" from here.

NOT MODELLED, stated rather than hidden: output tokens bill ~5x input, but
nothing in this codebase records them separately — ``claude_backend.py`` adds
``input_tokens + output_tokens`` into one ``tokens_used`` column before the
orchestrator or the DB ever sees them. So the fresh class is weighted 1.0 and
the output share of it is UNDER-priced. Closing that needs a schema change
(a fourth column), not a weight.
"""

from __future__ import annotations

#: Relative to one fresh input token. See the module docstring for what each
#: one is and for the one class this table cannot see.
FRESH_WEIGHT = 1.0
CACHE_CREATION_WEIGHT = 1.25
CACHE_READ_WEIGHT = 0.1


def weighted_tokens(
    *,
    tokens_used: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> int:
    """Spend in fresh-input-equivalent tokens — the unit every budget cap is in.

    KEYWORD-ONLY, for the same reason ``costOf`` in ``web/src/cost.js`` takes an
    object: the three classes differ by 12.5x and a positional call that
    transposed two of them would misprice a task silently. The class names are
    exactly the ledger's column names, so a caller can splat a usage dict.

    Floored to an int so the caps stay integer comparisons end to end.
    """
    return int(
        int(tokens_used or 0) * FRESH_WEIGHT
        + int(cache_read_tokens or 0) * CACHE_READ_WEIGHT
        + int(cache_creation_tokens or 0) * CACHE_CREATION_WEIGHT
    )


#: Weighted spend as a fraction of raw token count, measured over this
#: install's whole ledger on 2026-07-31: 193 tasks, 602 attempts,
#: 1,717,832,208 raw tokens against 340,995,449 weighted. Per-task it ranges
#: 0.122..0.697 (median 0.207) — this is the CORPUS aggregate, and it is used
#: for exactly one thing: converting a cap that was WRITTEN in raw tokens into
#: the weighted unit the caps are now in. It is never used to price actual
#: spend; real spend is always priced per class by `weighted_tokens`.
RAW_TO_WEIGHTED_RATIO = 0.1985

#: Marker stamped into `task.config` by every sanctioned write path, saying
#: "the token caps in this dict are already in the weighted unit". Its ABSENCE
#: is meaningful and load-bearing — see `raw_cap_as_weighted`.
BUDGET_UNIT_KEY = "budget_unit"
WEIGHTED_UNIT = "weighted"

#: The `task.config` keys that hold a TOKEN cap and therefore carry a unit.
#: `lifetime_attempts` is a count, not tokens, and is deliberately not here.
TOKEN_CAP_KEYS = frozenset({"lifetime_tokens", "attempt_tokens"})


def config_is_weighted(task_config: dict | None) -> bool:
    """Does this `task.config` state that its token caps are already weighted?

    Fails CLOSED: anything that is not the explicit marker — a missing key, a
    None config, a hand-edited dict, a value from an older release — reads as
    "these are raw tokens", which converts DOWN. Being wrong in that direction
    parks a task early and asks a human; being wrong in the other direction
    spends 5x the money with nobody watching.
    """
    if not isinstance(task_config, dict):
        return False
    return task_config.get(BUDGET_UNIT_KEY) == WEIGHTED_UNIT


def raw_cap_as_weighted(raw_cap: int) -> int:
    """A cap WRITTEN in raw tokens, re-expressed in the weighted unit.

    The cutover problem this exists for. When the caps changed unit
    (2026-07-31) there were 165 tasks on this install carrying a
    `task.config["lifetime_tokens"]` between 12,000,000 and 68,000,000, and
    162 carrying an `attempt_tokens` of 4,000,000 or 6,000,000 — all of them
    human raises written in RAW tokens. Read verbatim against a weighted gate
    those become ~5x the budget the human actually granted, and they are not
    stale history: 94 of the 165 are escalated/failed/paused tasks, i.e.
    exactly the population a mass retry picks up. Left alone, that retry would
    run with 1.388 BILLION raw-derived ceiling across the 91 escalated+failed
    tasks where ~275M was intended.

    A read-time conversion rather than a data migration, deliberately: it
    writes nothing to anyone's database, it needs no migration to have been
    run before the new code is safe, and it cannot half-apply. New raises are
    stamped with `BUDGET_UNIT_KEY` and skip this entirely, so the conversion
    applies once, to rows written before the cutover, and never compounds.
    """
    return int(int(raw_cap or 0) * RAW_TO_WEIGHTED_RATIO)


def class_breakdown(
    *,
    tokens_used: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> str:
    """The raw per-class numbers, for a human reading a parked task.

    The weighted figure alone is not honest reporting: it is the number the
    gate acted on, but it is not a number the operator can reconcile against a
    bill or against ``nh logs``. Both go in the blocker.
    """
    fresh = int(tokens_used or 0)
    read = int(cache_read_tokens or 0)
    creation = int(cache_creation_tokens or 0)
    return (
        f"raw {fresh + read + creation:,} = {fresh:,} fresh (x{FRESH_WEIGHT:g}) "
        f"+ {creation:,} cache-write (x{CACHE_CREATION_WEIGHT:g}) "
        f"+ {read:,} cache-read (x{CACHE_READ_WEIGHT:g})"
    )
