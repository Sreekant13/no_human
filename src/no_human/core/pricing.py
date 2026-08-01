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
  * The output premium is resolved PER MODEL, from the recorded model id —
    see ``MODEL_PRICES_USD_PER_MTOK`` and ``output_extra_weight``. What that
    lookup returns today, for every model in the table, is 4.0: every Claude
    model Anthropic publishes prices for bills output at exactly 5x input
    ($5/$25 Opus, $3/$15 Sonnet, $1/$5 Haiku, $10/$50 Fable). An earlier
    version of this paragraph said "Sonnet and Haiku bill at their own in/out
    ratios" in a way that implied those ratios DIFFER from Opus's. They do not.
    The table is keyed per model so that a future model which breaks the 5:1
    pattern prices correctly the day its id first appears in the ledger — not
    because any of today's ids disagree.

THE MODEL-BLINDNESS THAT REMAINS, stated plainly so it is not mistaken for
solved. The output PREMIUM is now per model; the fresh-input RATE is not. One
Opus reviewer token and one Sonnet coder token both weigh 1.0 here, and Opus
input costs $5/M against Sonnet's $3/M — so this unit is "fresh-input-
equivalent tokens *of the tier that spent them*", which is not a currency and
does not sum across tiers into one. Measured on this install's ledger (219
tasks, 684 attempts), repricing the classes by each model's input rate moves
the per-task weighted total by a median of 23–46% — direction and size both
depending on which model you anchor at 1.0 and what you charge the
unattributed utility tier. That is a budget-gate change, not a rounding
detail, and it is deliberately NOT made here.

``web/src/cost.js`` is the dollar-denominated twin and does NOT currently
agree with this file: it prices everything at one flat $3/1M fresh rate
(Sonnet's) with no per-model dimension at all, folds cache CREATION in at that
same fresh rate rather than 1.25x, and carries no output premium whatsoever.
The claim this docstring used to make — that cost.js "prices per model and per
class" and is "the place to go for a real invoice estimate" — was false on
both counts. Treat any dollar figure it prints as a floor, not an estimate.
"""

from __future__ import annotations

import logging
from collections import Counter

_log = logging.getLogger(__name__)

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

#: Published Anthropic LIST prices, USD per million tokens, as
#: ``(input, output)``, keyed on the model id EXACTLY AS IT IS RECORDED in
#: ``attempts.models``. Keying on the recorded string is the whole point: the
#: Opus tier moved from ``claude-opus-4-8`` to ``claude-opus-5`` on 2026-07-26,
#: and 441 of this install's 684 attempt rows still say ``claude-opus-4-8``.
#: Resolving through ``config.llm.*`` instead would silently re-price every one
#: of those historical rows at whatever the tier happens to be TODAY, which is
#: not what they cost.
#:
#: SOURCE, per row rather than in aggregate, because "cite where each came
#: from" is the difference between a price table and a guess. Every value below
#: is from Anthropic's published model/pricing table as carried by the bundled
#: ``claude-api`` skill (``SKILL.md`` § Current Models, cached 2026-06-24; the
#: live pages are ``platform.claude.com/docs/en/about-claude/models/overview``
#: and ``.../docs/en/pricing``). No value here is inferred, interpolated, or
#: extrapolated from a sibling model.
#:
#:   claude-opus-5      $5 / $25    skill table, "Claude Opus 5" row
#:   claude-opus-4-8    $5 / $25    skill table, "Claude Opus 4.8" row
#:   claude-opus-4-7    $5 / $25    skill table, "Claude Opus 4.7" row
#:   claude-opus-4-6    $5 / $25    skill table, "Claude Opus 4.6" row
#:   claude-sonnet-5    $3 / $15    skill table, "Claude Sonnet 5" row. That
#:                                  row also carries an introductory $2/$10
#:                                  through 2026-08-31. The intro price is NOT
#:                                  used here: this table exists to derive a
#:                                  RATIO, the intro price is 5:1 exactly as
#:                                  list is, and a promotional rate that lapses
#:                                  mid-ledger would make historical rows
#:                                  disagree with each other for no gain.
#:   claude-sonnet-4-6  $3 / $15    skill table, "Claude Sonnet 4.6" row
#:   claude-haiku-4-5   $1 / $5     skill table, "Claude Haiku 4.5" row
#:
#: Deliberately ABSENT: ``claude-fable-5``/``claude-mythos-5`` ($10/$50) and
#: Opus 5 fast mode ($10/$50) are published and are also 5:1, but no such id
#: has ever appeared in this ledger and none is reachable from `config.py`'s
#: four tiers. Adding ids this product cannot emit would be padding the table
#: to make it look more per-model than it is.
MODEL_PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

#: Every model id that reached `output_extra_weight` and was not in the table,
#: with a hit count. This exists so an unpriced id is VISIBLE rather than
#: silently absorbed into the fallback — the failure mode that made a
#: per-attempt brake inert on 27 of 27 tasks was exactly an unrecorded value
#: quietly pricing at nothing. Read it with `unknown_pricing_models()`.
_unknown_models: Counter[str] = Counter()


def unknown_pricing_models() -> dict[str, int]:
    """Model ids seen by the pricer that had no published price, and how often.

    A COPY, so a caller cannot mutate the counter it is reading. Empty is the
    expected steady state; a non-empty result means some tier is being priced
    at the fallback and somebody should add a sourced row above.
    """
    return dict(_unknown_models)


def _reset_unknown_pricing_models() -> None:
    """Clear the unknown-id counter. For tests, which must not leak into each
    other through module state; never call this from product code."""
    _unknown_models.clear()


def output_extra_weight(model: str | None) -> float:
    """The output PREMIUM for one model — its ``out/in`` ratio, minus the 1.0
    that ``tokens_used`` has already charged.

    Keyed on the RECORDED id (see ``MODEL_PRICES_USD_PER_MTOK``), never on
    live config, so a historical row keeps the price it was actually billed at.

    THE FALLBACK, and why it is what it is. ``None``/empty (11 of this
    install's 684 attempt rows carry ``models = '{}'``, and the utility tier
    has never been recorded at all) and any id absent from the table both fall
    back to ``OUTPUT_EXTRA_WEIGHT`` = 4.0 — the premium every model Anthropic
    currently publishes charges, and the value this whole file used before it
    was keyed per model. It is conservative in the only direction that matters:
    it is the HIGHEST premium in the table, so an unknown tier is never priced
    below a known one, and it can never be 0. A 0 or a missing multiplier here
    would price unknown output at nothing, which is the precise defect that
    once left a per-attempt brake inert on 27 of 27 tasks.

    An unknown NON-EMPTY id is also recorded in ``unknown_pricing_models()``
    and logged once — silence and a default would be the same bug wearing a
    different hat. ``None`` is not logged: it is the documented "this caller
    does not know the model" path, not an anomaly.
    """
    if not model:
        return OUTPUT_EXTRA_WEIGHT
    priced = MODEL_PRICES_USD_PER_MTOK.get(model)
    if priced is None:
        first_sighting = model not in _unknown_models
        _unknown_models[model] += 1
        if first_sighting:
            _log.warning(
                "no published price for model %r; pricing its output at the "
                "fallback premium %.1fx. Add a sourced row to "
                "core.pricing.MODEL_PRICES_USD_PER_MTOK.",
                model, OUTPUT_EXTRA_WEIGHT,
            )
        return OUTPUT_EXTRA_WEIGHT
    price_in, price_out = priced
    return price_out / price_in - 1.0


def weighted_tokens(
    *,
    tokens_used: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    output_tokens: int | None = None,
    model: str | None = None,
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

    ``model`` is the id that SPENT these tokens, as recorded in
    ``attempts.models`` — it selects the output premium (see
    ``output_extra_weight``). It is optional and defaults to ``None`` because
    the classes are summed across four tiers on almost every call site, and one
    id cannot describe four tiers; ``None`` takes the conservative fallback,
    which is what every one of those call sites got before this parameter
    existed. Pass it only where a single tier's spend is being priced on its
    own. Note that ``None`` here does NOT mean "free" — it means "unknown",
    and unknown prices at the highest published premium.

    Floored to an int so the caps stay integer comparisons end to end.
    """
    return int(
        int(tokens_used or 0) * FRESH_WEIGHT
        + int(output_tokens or 0) * output_extra_weight(model)
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
    model: str | None = None,
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

    ``model`` selects the output multiplier the same way it does in
    ``weighted_tokens``, and must be passed the same way at both call sites:
    the whole job of this string is to let a human reconcile the number the
    gate acted on, so printing a rate the gate did not charge would be worse
    than printing nothing.
    """
    fresh = int(tokens_used or 0)
    read = int(cache_read_tokens or 0)
    creation = int(cache_creation_tokens or 0)
    out = int(output_tokens or 0)
    out_rate = FRESH_WEIGHT + output_extra_weight(model)
    out_note = f" of which {out:,} output (x{out_rate:g})" if out else ""
    return (
        f"raw {fresh + read + creation:,} = {fresh:,} fresh (x{FRESH_WEIGHT:g})"
        f"{out_note} "
        f"+ {creation:,} cache-write (x{CACHE_CREATION_WEIGHT:g}) "
        f"+ {read:,} cache-read (x{CACHE_READ_WEIGHT:g})"
    )
