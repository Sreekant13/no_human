"""``pricing.usd_cost``: the float twin of ``weighted_tokens``, in dollars.

WHY THIS FILE EXISTS. ``web/src/cost.js`` priced every attempt — Claude AND
Codex — at one hardcoded Anthropic rate (`$3/1K` fresh, `$0.3/1K` cache-read).
That was wrong the moment ``core/pricing.py`` gained per-model OpenAI rows: a
`gpt-5.3-codex` attempt ($1.75/$14 published) rendered on the board as if it
had been billed at Sonnet's $3/$15. ``usd_cost`` is the ONE place a dollar
figure is computed now — the board (``web/src/cost.js``) only formats what
the API sends it. This file pins the arithmetic; ``tests/test_api.py`` pins
that the API actually calls it.

THE REGRESSION PIN. `test_claude_regression_pin` fixes a Claude-only, fresh
+ cache-read attempt (no cache-creation, no output split) at the exact dollar
figure the OLD flat-rate JS `costOf` produced for the same buckets — proof
this change does not reprice a Claude attempt that predates the output-share
and cache-creation columns. It is deliberately NOT the same guarantee for a
row that DOES carry cache-creation: `weighted_tokens` already prices creation
at 1.25x (the JS module never did), so a Claude total that includes creation
is deliberately higher now, not a bug.
"""

from __future__ import annotations

import pytest

from no_human.core.pricing import (
    FALLBACK_PRICE_NAME,
    FALLBACK_PRICING_MODEL,
    MODEL_PRICES_USD_PER_MTOK,
    _reset_unknown_pricing_models,
    input_price_usd_per_mtok,
    unknown_pricing_models,
    usd_cost,
    weighted_tokens,
)

CLAUDE = "claude-sonnet-5"
CODEX = "gpt-5.3-codex"
UNPRICED = "gpt-5-codex"  # deliberately absent from MODEL_PRICES_USD_PER_MTOK


@pytest.fixture(autouse=True)
def _clean_unknown_counter():
    """Module-level counter — leaking it between tests would let one test's
    unknown id satisfy another test's assertion."""
    _reset_unknown_pricing_models()
    yield
    _reset_unknown_pricing_models()


def test_usd_cost_is_weighted_tokens_at_the_models_input_rate():
    # weighted = 1_000_000*1.0 (fresh) + 500_000*0.1 (read) + 200_000*1.25
    # (creation) + 100_000*4.0 (Claude's uniform 5:1 output premium)
    #          = 1,000,000 + 50,000 + 250,000 + 400,000 = 1,700,000
    weighted = weighted_tokens(
        tokens_used=1_000_000,
        cache_read_tokens=500_000,
        cache_creation_tokens=200_000,
        output_tokens=100_000,
        model=CLAUDE,
    )
    assert weighted == 1_700_000
    rate, label = input_price_usd_per_mtok(CLAUDE)
    assert (rate, label) == (3.0, CLAUDE)
    dollars = usd_cost(
        tokens_used=1_000_000,
        cache_read_tokens=500_000,
        cache_creation_tokens=200_000,
        output_tokens=100_000,
        model=CLAUDE,
    )
    assert dollars == pytest.approx(weighted * rate / 1_000_000.0)
    assert dollars == pytest.approx(5.1)


def test_usd_cost_prices_codex_at_its_own_cheaper_rate_not_claudes():
    # gpt-5.3-codex is $1.75/$14 — cheaper input, steeper output ratio (8:1)
    # than any Claude row. Pricing it at Sonnet's $3/$15 is exactly the bug
    # web/src/cost.js had.
    claude_cost = usd_cost(tokens_used=1_000_000, model=CLAUDE)
    codex_cost = usd_cost(tokens_used=1_000_000, model=CODEX)
    assert claude_cost == pytest.approx(3.0)
    assert codex_cost == pytest.approx(1.75)
    assert codex_cost != claude_cost


def test_no_price_number_is_introduced():
    """Every rate `usd_cost` can return already exists in
    `MODEL_PRICES_USD_PER_MTOK` — this change reads that table, it does not
    add a new one."""
    known_input_rates = {p[0] for p in MODEL_PRICES_USD_PER_MTOK.values()}
    for model in list(MODEL_PRICES_USD_PER_MTOK) + [None, "", UNPRICED]:
        rate, _label = input_price_usd_per_mtok(model)
        assert rate in known_input_rates


def test_usd_cost_never_zero_for_nonzero_tokens_unknown_model():
    dollars = usd_cost(tokens_used=1_000_000, model=UNPRICED)
    assert dollars > 0
    rate, label = input_price_usd_per_mtok(UNPRICED)
    assert label == FALLBACK_PRICE_NAME
    assert rate == MODEL_PRICES_USD_PER_MTOK[FALLBACK_PRICING_MODEL][0]
    # visible, not silently absorbed — same contract as output_extra_weight
    assert unknown_pricing_models().get(UNPRICED) == 1


def test_input_price_usd_per_mtok_none_and_empty_use_the_fallback_silently():
    for model in (None, ""):
        rate, label = input_price_usd_per_mtok(model)
        assert (rate, label) == (3.0, FALLBACK_PRICE_NAME)
    # None/"" are the documented "no model recorded" case, not an anomaly —
    # they must not be counted as an unknown SIGHTING the way a real
    # unpriced id is.
    assert unknown_pricing_models() == {}


def test_claude_regression_pin():
    """Fresh + cache-read only (no creation, no output split) — the exact
    shape the old flat-rate JS `costOf` priced. 1M fresh @ $3/Mtok = $3.00;
    1M cache-read @ 0.1x = $0.30. Same $3.30 the deleted JS produced for the
    same two buckets."""
    dollars = usd_cost(tokens_used=1_000_000, cache_read_tokens=1_000_000, model=CLAUDE)
    assert dollars == pytest.approx(3.30)


def test_weighted_tokens_is_unchanged_by_the_usd_addition():
    """`usd_cost` must not have touched the budget-gate function it mirrors."""
    assert weighted_tokens(tokens_used=1_000_000, model=CLAUDE) == 1_000_000
    assert weighted_tokens(
        tokens_used=1_000_000,
        cache_read_tokens=500_000,
        cache_creation_tokens=200_000,
        output_tokens=100_000,
        model=CLAUDE,
    ) == 1_700_000
