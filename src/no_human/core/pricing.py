"""Token-class price weights — the ONE Python price table.

A token is not a token. Anthropic bills the classes the ledger records at
different rates, relative to one FRESH INPUT token:

  * fresh input        1.0   (the ``tokens_used`` column, minus the output
                              share below)
  * fresh OUTPUT       5.0   (Opus 5: $5/M in, $25/M out — carried here as
                              ``OUTPUT_EXTRA_WEIGHT`` = 4.0, the PREMIUM over
                              the 1.0 ``tokens_used`` already charges it)
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

THE OUTPUT SHARE, and what is still missing from it. Output bills ~5x input
and for a long time nothing here recorded it: ``claude_backend.py`` added
``input_tokens + output_tokens`` into one ``tokens_used`` column before the
orchestrator or the DB ever saw the split, so the whole fresh class was
weighted 1.0 and its output share was under-priced. The remedy this docstring
asked for — "a schema change (a fourth column), not a weight" — is now in
place: ``attempts.output_tokens`` (and its ``review_``/``plan_``/``utility_``
siblings) records how much of ``tokens_used`` was output, and
``weighted_tokens`` charges the 4.0 premium on it.

Two limits, so this paragraph does not read as a clean bill of health:

  * It is not retroactive and cannot be. The split was discarded AT CAPTURE,
    so there is nothing to backfill from; every attempt row written before the
    column existed holds NULL, and NULL prices at the old 1.0 — a known
    under-count on historical data, not a silent one. NULL is deliberate: a 0
    there would assert those attempts emitted no output at all.
  * The 5x ratio is Opus 5's. Sonnet and Haiku bill at their own in/out
    ratios, and this table is model-blind — one weight for every tier. It is
    an approximation chosen to be honest about the biggest error (a 5x class
    priced at 1x), not a per-model price list.

``web/src/cost.js``, the dollar-denominated twin below, prices per model and
per class in dollars and is the place to go for a real invoice estimate.
"""

from __future__ import annotations

#: Relative to one fresh input token. See the module docstring for what each
#: one is and for what this table approximates rather than knows.
FRESH_WEIGHT = 1.0
CACHE_CREATION_WEIGHT = 1.25
CACHE_READ_WEIGHT = 0.1

#: The output PREMIUM, not the output rate. Output bills 5.0x fresh input, and
#: `tokens_used` — which is input+output — has already charged it 1.0, so what
#: is left to add is 4.0. Spelling it as the difference is what keeps
#: `output_tokens` a SUBSET of `tokens_used` rather than a fourth addend
#: beside it: pass both and the total is priced once, correctly.
OUTPUT_EXTRA_WEIGHT = 4.0


def weighted_tokens(
    *,
    tokens_used: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    output_tokens: int | None = None,
) -> int:
    """Spend in fresh-input-equivalent tokens — the unit every budget cap is in.

    KEYWORD-ONLY, for the same reason ``costOf`` in ``web/src/cost.js`` takes an
    object: the classes differ by up to 50x and a positional call that
    transposed two of them would misprice a task silently. The class names are
    exactly the ledger's column names, so a caller can splat a usage dict.

    ``output_tokens`` is the output SHARE of ``tokens_used``, not a fourth
    bucket beside it — see ``OUTPUT_EXTRA_WEIGHT``. ``None`` means the split
    was never recorded (every attempt row that predates the column, and any
    tier that reported no usage block); it prices identically to 0, which is
    the OLD arithmetic exactly. That is the compatibility guarantee this
    function makes: an existing caller that has not been taught the keyword,
    and any historical row, gets the number it always got.

    Floored to an int so the caps stay integer comparisons end to end.
    """
    return int(
        int(tokens_used or 0) * FRESH_WEIGHT
        + int(output_tokens or 0) * OUTPUT_EXTRA_WEIGHT
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
    output_tokens: int | None = None,
) -> str:
    """The raw per-class numbers, for a human reading a parked task.

    The weighted figure alone is not honest reporting: it is the number the
    gate acted on, but it is not a number the operator can reconcile against a
    bill or against ``nh logs``. Both go in the blocker.

    The output share is reported INSIDE the fresh term ("of which N output"),
    never as a fourth addend, because that is what it is — adding it to the
    total here would print a raw number that matches nothing else on any
    surface. It is omitted entirely when it is 0 or unknown, so a task with no
    recorded split reads exactly as it did before the column existed rather
    than gaining a misleading "0 output".
    """
    fresh = int(tokens_used or 0)
    read = int(cache_read_tokens or 0)
    creation = int(cache_creation_tokens or 0)
    out = int(output_tokens or 0)
    out_note = (
        f" of which {out:,} output (x{FRESH_WEIGHT + OUTPUT_EXTRA_WEIGHT:g})"
        if out else ""
    )
    return (
        f"raw {fresh + read + creation:,} = {fresh:,} fresh (x{FRESH_WEIGHT:g})"
        f"{out_note} "
        f"+ {creation:,} cache-write (x{CACHE_CREATION_WEIGHT:g}) "
        f"+ {read:,} cache-read (x{CACHE_READ_WEIGHT:g})"
    )
