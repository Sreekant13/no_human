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
    lookup returns for every CLAUDE model in the table is a uniform 4.0: every
    Claude model Anthropic publishes prices for bills output at exactly 5x
    input ($5/$25 Opus, $3/$15 Sonnet, $1/$5 Haiku, $10/$50 Fable). An earlier
    version of this paragraph said "Sonnet and Haiku bill at their own in/out
    ratios" in a way that implied those ratios DIFFER from Opus's. They do not.
    The table is keyed per model so that a future model which breaks the 5:1
    pattern prices correctly the day its id first appears in the ledger — and
    one now has: the OpenAI ids sourced for the Codex backend (see the ``#:``
    block below the table) do NOT share the 5:1 ratio, so this lookup no
    longer returns 4.0 "for every model in the table" as a whole — see
    "THE OPENAI SIDE" below for what that changes and what stays true.

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

THE OPENAI SIDE, added when the Codex backend went live. Every ``gpt-*`` row
in ``MODEL_PRICES_USD_PER_MTOK`` is a per-token USD rate read from an
OpenAI-hosted pricing page (see the ``#:`` block above the table for the exact
URL, date, and row per id — never guessed, never interpolated from a sibling).
None of them share the Anthropic family's 5:1 ratio: ``gpt-5.3-codex`` is 8:1,
so its ``output_extra_weight`` (7.0) is ABOVE ``OUTPUT_EXTRA_WEIGHT`` (4.0).
"The fallback is never cheaper than any priced model" held only within the
Claude family it was derived from until the fallback was keyed per backend
(``fallback_output_extra_weight``; ``output_extra_weight``/``weighted_tokens``/
``class_breakdown`` all take a keyword-only ``backend``): a genuinely unpriced
id (one this table has no row for) on the ``"codex"`` backend now takes the
MAX premium over this table's own OpenAI rows — computed live at call time
from ``MODEL_PRICES_USD_PER_MTOK``, never a copied literal, so a newly sourced
OpenAI row raises the fallback with it — which restores the invariant on that
backend too. Every other backend (``"claude"``, ``"local"``, and unset/blank)
keeps the plain 4.0 fallback unchanged. An id in either state is still
surfaced by ``unknown_pricing_models()`` rather than silently under-priced,
which remains the reporting improvement this whole change exists to make for
the ids it CAN price, and the honest limit for the ones it still cannot.

A run billed by a flat ChatGPT subscription plan gets NO row here and no
per-token price at all — not ``0.0`` (that would price its output at nothing,
the exact inert-brake defect the fallback exists to prevent) and not a
sentinel (every consumer of this dict would have to learn a new shape).
``gpt-5.5`` and ``gpt-5.6-terra`` are reachable BOTH as a priced Platform-API
id and as a flat-plan ChatGPT session; the row above prices the *id*, not the
billing mode, so a subscription-billed run of either id is over-stated by
whatever this table charges its API-priced sibling — a known residual,
recorded rather than silently fixed, because ``attempts.models`` does not
record which mode paid for a run and teaching this lookup to care is ticket
``d35aa60e``'s auth-mode-plumbing territory, not this one's. An id that is
ONLY reachable on a flat plan, with no Platform-API price published at all,
gets no row either and falls to the visible ``OUTPUT_EXTRA_WEIGHT`` fallback,
surfaced the same way — an honest gap, never a fabricated dollar figure.
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
#: live page is ``https://platform.claude.com/docs/en/about-claude/models/overview``
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
#:
#: --- OpenAI (Codex backend) ---
#: Published OpenAI Platform-API LIST prices, USD per million tokens, for the
#: "short context" (<272K tokens) tier — the same tier every existing Codex
#: coding attempt runs in. Sourced from OpenAI's own hosted pricing page,
#: https://developers.openai.com/api/docs/pricing (the page
#: ``platform.openai.com/docs/pricing`` redirects to), read 2026-08-23. Only
#: ids `codex exec` actually resolved on 2026-08-22 (per ticket d35aa60e's
#: measurement) are listed; no id is added on a sibling-model guess.
#:
#:   gpt-5.3-codex   $1.75 / $14.00   https://developers.openai.com/api/docs/pricing, "Codex" row under Specialized models, read 2026-08-23
#:   gpt-5.4         $2.50 / $15.00   https://developers.openai.com/api/docs/pricing, "gpt-5.4 (<272K context length)" row, read 2026-08-23
#:   gpt-5.5         $5.00 / $30.00   https://developers.openai.com/api/docs/pricing, "gpt-5.5 (<272K context length)" row, read 2026-08-23
#:   gpt-5.6-terra   $2.00 / $12.00   https://developers.openai.com/api/docs/pricing, "gpt-5.6-terra" row (short-context column), read 2026-08-23
#:
#: Deliberately ABSENT (OpenAI): ``gpt-5-codex`` — the current
#: `llm.codex_model` default — plus ``gpt-5.1-codex``, ``gpt-5.1-codex-max``,
#: and ``gpt-5.2-codex``. All four returned "Model not found" through
#: `codex exec` when measured 2026-08-22, and none appears on the pricing page
#: fetched 2026-08-23 either: they are retired, not merely unlisted, and a row
#: for a retired id would price a model nothing can ever bill again. See the
#: module docstring ("THE OPENAI SIDE") for the subscription-billing case,
#: which also gets no row here.
MODEL_PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    # --- OpenAI (Codex backend) --- see the "#:" block above for citations.
    "gpt-5.3-codex": (1.75, 14.00),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.5": (5.00, 30.00),
    "gpt-5.6-terra": (2.00, 12.00),
}

#: The USD fallback for an unpriced or unrecorded model — `claude-sonnet-5`'s
#: own published row, reused rather than a new number invented for this
#: purpose. `output_extra_weight`'s fallback (`OUTPUT_EXTRA_WEIGHT` = 4.0)
#: exists for the same reason: unknown must never price at 0.0, which is the
#: inert-brake defect this whole module exists to keep out. `FALLBACK_PRICE_NAME`
#: is the label a caller shows beside the number, so "priced at $3/Mtok
#: because that's what claude-sonnet-5 costs" is never mistaken for "priced at
#: $3/Mtok because that's what THIS model costs".
FALLBACK_PRICING_MODEL = "claude-sonnet-5"
FALLBACK_PRICE_NAME = f"fallback:{FALLBACK_PRICING_MODEL}"

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


#: Every id this table carries for the Claude family is spelled with this
#: prefix (see ``MODEL_PRICES_USD_PER_MTOK``'s citation block) — the same
#: prefix `tests/test_pricing.py`'s ``OPENAI_IDS`` derives ``not
#: k.startswith("claude-")`` from. Named once so `_rows_for_backend` and this
#: prefix cannot drift apart from a hand-typed literal in two places.
CLAUDE_ID_PREFIX = "claude-"


def _rows_for_backend(backend: str | None) -> dict[str, tuple[float, float]]:
    """The subset of ``MODEL_PRICES_USD_PER_MTOK`` a fallback for *backend*
    may draw its premium from.

    Only ``"codex"`` currently has more than one vendor family in the table,
    so it is the only backend with a non-empty result: every row that is NOT
    a Claude id (``CLAUDE_ID_PREFIX``) — today that is exactly the OpenAI
    rows, but this reads the prefix rather than naming the vendor, so a
    future non-Claude row prices correctly without touching this function.
    Every other backend (``"claude"``, ``"local"``, unset/blank, or an
    unrecognised name) returns ``{}`` — deliberately, so
    ``fallback_output_extra_weight`` falls through to the plain
    ``OUTPUT_EXTRA_WEIGHT`` for them instead of silently drawing a premium
    from a vendor that backend does not run.
    """
    if backend != "codex":
        return {}
    return {
        model_id: rates
        for model_id, rates in MODEL_PRICES_USD_PER_MTOK.items()
        if not model_id.startswith(CLAUDE_ID_PREFIX)
    }


def fallback_output_extra_weight(*, backend: str | None = None) -> float:
    """The premium an UNRECORDED/unpriced model on *backend* falls back to.

    Plain ``OUTPUT_EXTRA_WEIGHT`` (4.0) for every backend except ``"codex"``:
    that value was the highest premium in the table while it held only the
    Claude family, and stopped being that the moment a non-5:1 vendor
    entered it (``gpt-5.3-codex`` publishes 8:1, premium 7.0) — priced BELOW
    its true rate is the one thing a fallback must never do. For
    ``backend="codex"`` this instead returns the MAX premium over that
    backend's own priced rows (``_rows_for_backend``), computed live from
    ``MODEL_PRICES_USD_PER_MTOK`` at call time — never a copied literal, so a
    newly sourced OpenAI row (or one that is repriced) raises or lowers this
    fallback with it automatically.

    Always at least ``OUTPUT_EXTRA_WEIGHT``: a codex table with no OpenAI
    rows at all (e.g. under a test's ``monkeypatch.setitem`` that strips them)
    has nothing to compute a max over and must not fall through to 0 — it
    falls back to the plain 4.0 instead, the same floor every other backend
    uses.
    """
    rows = _rows_for_backend(backend)
    if not rows:
        return OUTPUT_EXTRA_WEIGHT
    premiums = (price_out / price_in - 1.0 for price_in, price_out in rows.values())
    return max(OUTPUT_EXTRA_WEIGHT, max(premiums))


def output_extra_weight(model: str | None, *, backend: str | None = None) -> float:
    """The output PREMIUM for one model — its ``out/in`` ratio, minus the 1.0
    that ``tokens_used`` has already charged.

    Keyed on the RECORDED id (see ``MODEL_PRICES_USD_PER_MTOK``), never on
    live config, so a historical row keeps the price it was actually billed at.

    THE FALLBACK, and why it is what it is. ``None``/empty (11 of this
    install's 684 attempt rows carry ``models = '{}'``, and the utility tier
    has never been recorded at all) and any id absent from the table both fall
    back to ``fallback_output_extra_weight(backend=backend)`` — plain
    ``OUTPUT_EXTRA_WEIGHT`` = 4.0 for every backend except ``"codex"``, which
    fell out of date the moment a second, non-5:1 vendor entered the table:
    ``gpt-5.3-codex`` publishes 8:1 (premium 7.0), so an UNRECORDED id on the
    ``"codex"`` backend priced at the plain 4.0 would be priced BELOW its true
    rate — exactly the "an unknown tier is never priced below a known one"
    invariant this fallback exists to hold. ``backend="codex"`` instead takes
    the MAX premium over this table's own OpenAI rows, computed live at call
    time (never a copied literal), so the invariant holds on that backend too;
    every other backend — ``"claude"``, ``"local"``, unset/blank — keeps the
    plain 4.0 unchanged. What the fallback still guarantees, on every backend,
    is that it can never be 0. A 0 or a missing multiplier here would price
    unknown output at nothing, which is the precise defect that once left a
    per-attempt brake inert on 27 of 27 tasks.

    An unknown NON-EMPTY id is also recorded in ``unknown_pricing_models()``
    and logged once — silence and a default would be the same bug wearing a
    different hat. ``None`` is not logged: it is the documented "this caller
    does not know the model" path, not an anomaly.

    ``backend`` is IGNORED once ``model`` is priced: a row in
    ``MODEL_PRICES_USD_PER_MTOK`` is the actual billed rate, and a backend
    hint can never override a known price.
    """
    if not model:
        return fallback_output_extra_weight(backend=backend)
    priced = MODEL_PRICES_USD_PER_MTOK.get(model)
    if priced is None:
        fallback = fallback_output_extra_weight(backend=backend)
        first_sighting = model not in _unknown_models
        _unknown_models[model] += 1
        if first_sighting:
            _log.warning(
                "no published price for model %r; pricing its output at the "
                "fallback premium %.1fx. Add a sourced row to "
                "core.pricing.MODEL_PRICES_USD_PER_MTOK.",
                model, fallback,
            )
        return fallback
    price_in, price_out = priced
    return price_out / price_in - 1.0


def weighted_tokens(
    *,
    tokens_used: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    output_tokens: int | None = None,
    model: str | None = None,
    backend: str | None = None,
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
    the classes are summed across every role in ``db.USAGE_ROLES`` (six: coder,
    reviewer, planner, utility, supervisor, distill) on almost every call site,
    and one id cannot describe six roles; ``None`` takes the conservative
    fallback, which is what every one of those call sites got before this
    parameter existed. Pass it only where a single role's spend is being
    priced on its own. Note that ``None`` here does NOT mean "free" — it
    means "unknown", and unknown prices at the highest published premium.

    ``backend`` selects which fallback an UNPRICED ``model`` (or a ``None``
    one) takes — see ``output_extra_weight``/``fallback_output_extra_weight``.
    It is ignored once ``model`` is a priced id.

    Floored to an int so the caps stay integer comparisons end to end.
    """
    return int(
        int(tokens_used or 0) * FRESH_WEIGHT
        + int(output_tokens or 0) * output_extra_weight(model, backend=backend)
        + int(cache_read_tokens or 0) * CACHE_READ_WEIGHT
        + int(cache_creation_tokens or 0) * CACHE_CREATION_WEIGHT
    )


def input_price_usd_per_mtok(model: str | None) -> tuple[float, str]:
    """The fresh-input USD/Mtok rate for one model, and the label to show for it.

    Keyed on the recorded id, exactly like ``output_extra_weight`` — never
    live config, for the same reason: a historical row keeps the price it was
    actually billed at even after `config.py`'s tiers move on.

    Returns ``(rate, label)``. When ``model`` is priced, ``label`` is the bare
    id — the honest "this is what it actually cost" case. When it is not
    (``None``/empty, or a non-empty id absent from the table),
    ``FALLBACK_PRICING_MODEL``'s own rate is returned under
    ``FALLBACK_PRICE_NAME``, never a bare id and never 0.0 — a 0 here would
    price real spend at nothing, the same inert-brake defect
    ``output_extra_weight``'s fallback exists to prevent.

    Recording an unpriced id into ``unknown_pricing_models()`` is
    ``output_extra_weight``'s job, not this function's: ``usd_cost`` calls
    both for the same model, and counting the same sighting twice would
    double the hit counter for one attempt's one model.
    """
    if model:
        priced = MODEL_PRICES_USD_PER_MTOK.get(model)
        if priced is not None:
            return priced[0], model
    return MODEL_PRICES_USD_PER_MTOK[FALLBACK_PRICING_MODEL][0], FALLBACK_PRICE_NAME


def usd_cost(
    *,
    tokens_used: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    output_tokens: int | None = None,
    model: str | None = None,
) -> float:
    """Spend in USD — the float twin of ``weighted_tokens``, priced per model.

    Same classes, same KEYWORD-ONLY shape, same reason: a positional call
    could transpose two buckets that differ by up to 50x. The difference is
    the unit — ``weighted_tokens`` returns a dimensionless, model-blind
    fresh-input-equivalent count (see its docstring: "THE MODEL-BLINDNESS
    THAT REMAINS"); this multiplies the identical weighted sum by
    ``model``'s own published fresh-input rate, so two attempts with the same
    weighted total can (correctly) show different dollars.

    ``output_extra_weight(model)`` supplies the output premium exactly as
    ``weighted_tokens`` does; ``input_price_usd_per_mtok(model)`` supplies the
    rate that turns the weighted sum into dollars. Both are called once each,
    on the same ``model``, so an unpriced id is recorded into
    ``unknown_pricing_models()`` exactly once per call.

    Never zero for nonzero tokens priced at an unknown model: the fallback
    rate is ``claude-sonnet-5``'s published $3/Mtok, never 0.0.

    Rounded to 6dp — dollars, not fresh-input-equivalent tokens, so an int
    floor would be wrong (it would print $0.00 for a $0.40 attempt).
    """
    weighted = (
        float(tokens_used or 0) * FRESH_WEIGHT
        + float(output_tokens or 0) * output_extra_weight(model)
        + float(cache_read_tokens or 0) * CACHE_READ_WEIGHT
        + float(cache_creation_tokens or 0) * CACHE_CREATION_WEIGHT
    )
    rate, _label = input_price_usd_per_mtok(model)
    return round(weighted * rate / 1_000_000.0, 6)


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


def override_inverted(raw_cap: int | None, weighted_default: int) -> bool:
    """Does converting this pre-cutover RAW override flip a raise into a cut?

    R1, the defect that killed the August funnel. The `no_human` repo profile
    carried ``default_lifetime_tokens = 12,000,000``, written before the
    cutover, so unmarked, so converted: 2,382,000 — BELOW the ungranted
    4,000,000 default. An operator who types a number ABOVE the default is
    unambiguously asking for MORE than the default; a conversion that turns
    that into 40% LESS has not re-expressed their intent, it has inverted it.
    32 of 33 August tasks ran against `/2,382,000` and the median one died
    exactly there.

    The predicate is the sign flip and nothing else: raise as typed
    (``raw_cap > default``), cut as read (``converted < default``). It is
    deliberately NOT ``max(converted, default)``, because that would also raise
    a value that was small in BOTH units — a deliberate lowering, which the
    caps must keep supporting: every budget test in the suite writes a tiny
    unmarked cap, and `_stored_token_cap` has always honoured it. Those are
    untouched here; only the contradiction is.

    Nothing about this guesses at intent from a magic value or a date. It reads
    the operator's own number against the operator's own default and refuses
    the one interpretation that is self-contradictory.
    """
    return int(raw_cap or 0) > int(weighted_default or 0) > raw_cap_as_weighted(raw_cap)


def class_breakdown(
    *,
    tokens_used: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    output_tokens: int | None = None,
    model: str | None = None,
    backend: str | None = None,
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
    than printing nothing. ``backend`` must likewise match whatever the same
    call's ``weighted_tokens``/``output_extra_weight`` used, for the same
    reason — the printed rate must equal the charged rate.
    """
    fresh = int(tokens_used or 0)
    read = int(cache_read_tokens or 0)
    creation = int(cache_creation_tokens or 0)
    out = int(output_tokens or 0)
    out_rate = FRESH_WEIGHT + output_extra_weight(model, backend=backend)
    out_note = f" of which {out:,} output (x{out_rate:g})" if out else ""
    return (
        f"raw {fresh + read + creation:,} = {fresh:,} fresh (x{FRESH_WEIGHT:g})"
        f"{out_note} "
        f"+ {creation:,} cache-write (x{CACHE_CREATION_WEIGHT:g}) "
        f"+ {read:,} cache-read (x{CACHE_READ_WEIGHT:g})"
    )
