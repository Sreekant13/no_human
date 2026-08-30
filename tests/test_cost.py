"""``core.cost.attempt_cost`` / ``attempts_cost`` — the ONE place a dollar
figure for an attempt (or a list of them) is computed from a raw DB row.

``tests/test_pricing_usd.py`` pins the underlying per-token arithmetic
(``pricing.usd_cost``); this file pins how a WHOLE ATTEMPT ROW — every role in
``db.USAGE_ROLES``, each possibly at its own recorded model — gets summed and
labeled, and the degenerate-input contract documented in ``core/cost.py``'s
module docstring (missing/unparseable ``models``, an idle role, ``None``/
empty rows).
"""

from __future__ import annotations

import pytest

from no_human.core.cost import attempt_cost, attempts_cost
from no_human.core.pricing import FALLBACK_PRICE_NAME

CLAUDE = "claude-sonnet-5"
CLAUDE_OPUS = "claude-opus-4-8"
CODEX = "gpt-5.3-codex"
UNPRICED = "gpt-5-codex"  # deliberately absent from MODEL_PRICES_USD_PER_MTOK


def test_attempt_cost_prices_a_codex_coder_row_at_its_own_rate():
    """The bug this module exists to fix: web/src/cost.js priced every
    attempt at one flat Anthropic rate, which was wrong the moment a Codex
    attempt appeared. gpt-5.3-codex is $1.75/Mtok, not Sonnet's $3."""
    row = {"tokens_used": 1_000_000, "models": {"coder": CODEX}}
    dollars, label = attempt_cost(row)
    assert dollars == pytest.approx(1.75)
    assert label == CODEX


def test_attempt_cost_sums_coder_and_reviewer_at_their_own_models():
    """A Codex coder reviewed by Claude is real and common: each role prices
    at its own recorded model, and the shared label is 'mixed' when priced
    roles disagree — collapsing to one would misreport which model spent
    which dollar."""
    row = {
        "tokens_used": 1_000_000,
        "review_tokens_used": 1_000_000,
        "models": {"coder": CODEX, "reviewer": CLAUDE_OPUS},
    }
    dollars, label = attempt_cost(row)
    assert dollars == pytest.approx(1.75 + 5.0)
    assert label == "mixed"


def test_attempt_cost_idle_role_does_not_force_mixed():
    """utility has a model recorded but spent nothing this attempt — its
    (possibly different) model must not affect the mixed/agree call for the
    role that actually spent money."""
    row = {
        "tokens_used": 1_000_000,
        "models": {"coder": CLAUDE, "utility": "claude-haiku-4-5"},
    }
    dollars, label = attempt_cost(row)
    assert dollars == pytest.approx(3.0)
    assert label == CLAUDE


def test_attempt_cost_missing_models_column_falls_back_never_zero():
    """684 of this install's attempt rows predate the `models` column
    entirely — NULL there must still price nonzero, at the visible fallback,
    never 0.0 (which would assert those attempts cost nothing)."""
    row = {"tokens_used": 1_000_000}
    dollars, label = attempt_cost(row)
    assert dollars == pytest.approx(3.0)
    assert label == FALLBACK_PRICE_NAME


def test_attempt_cost_unparseable_models_json_falls_back():
    row = {"tokens_used": 1_000_000, "models": "not json"}
    dollars, label = attempt_cost(row)
    assert dollars == pytest.approx(3.0)
    assert label == FALLBACK_PRICE_NAME


def test_attempt_cost_empty_object_models_falls_back():
    row = {"tokens_used": 1_000_000, "models": "{}"}
    dollars, label = attempt_cost(row)
    assert dollars == pytest.approx(3.0)
    assert label == FALLBACK_PRICE_NAME


def test_attempt_cost_unpriced_model_id_is_visible_not_silent():
    """A genuinely unknown model id must still say so via the fallback
    label — never render as if it were the (cheaper or costlier) id that was
    actually recorded."""
    row = {"tokens_used": 1_000_000, "models": {"coder": UNPRICED}}
    dollars, label = attempt_cost(row)
    assert dollars > 0
    assert label == FALLBACK_PRICE_NAME
    assert label != UNPRICED


def test_attempt_cost_none_row_is_zero_not_an_error():
    assert attempt_cost(None) == (0.0, FALLBACK_PRICE_NAME)
    assert attempt_cost({}) == (0.0, FALLBACK_PRICE_NAME)


def test_attempt_cost_idle_attempt_is_zero_fallback_not_none():
    """A row where every role's token columns are 0/NULL contributes no
    priced role at all — attempt_cost still returns a number (0.0), never
    raises for lack of a label to report."""
    row = {"models": {"coder": CLAUDE}}
    assert attempt_cost(row) == (0.0, FALLBACK_PRICE_NAME)


def test_attempts_cost_none_and_empty_mean_no_attempts_yet():
    """(None, None) — distinct from '(0.0, ...)', which would say the
    attempts that exist spent nothing. matches every sibling total_* field."""
    assert attempts_cost(None) == (None, None)
    assert attempts_cost([]) == (None, None)


def test_attempts_cost_all_idle_attempts_is_zero_not_none():
    """Once at least one attempt row exists, 'spent nothing' is a real,
    representable fact — 0.0, not None."""
    rows = [{"models": {"coder": CLAUDE}}, {}]
    total, label = attempts_cost(rows)
    assert total == 0.0
    assert label == FALLBACK_PRICE_NAME


def test_attempts_cost_sums_across_attempts_same_model():
    rows = [
        {"tokens_used": 1_000_000, "models": {"coder": CLAUDE}},
        {"tokens_used": 2_000_000, "models": {"coder": CLAUDE}},
    ]
    total, label = attempts_cost(rows)
    assert total == pytest.approx(9.0)
    assert label == CLAUDE


def test_attempts_cost_mixed_across_attempts_when_models_differ_between_attempts():
    """Attempt 1 was Codex, attempt 2 was retried on Claude — a real,
    escalation-driven shape, not an edge case."""
    rows = [
        {"tokens_used": 1_000_000, "models": {"coder": CLAUDE}},
        {"tokens_used": 1_000_000, "models": {"coder": CODEX}},
    ]
    total, label = attempts_cost(rows)
    assert total == pytest.approx(3.0 + 1.75)
    assert label == "mixed"
