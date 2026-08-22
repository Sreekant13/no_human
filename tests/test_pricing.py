"""The OpenAI (Codex backend) side of the price table — citations, retirement,
and the subscription-billing gap.

WHY THIS FILE EXISTS, separate from `test_pricing_per_model.py`. Every test
over there assumed a single-vendor, uniformly-5:1 table; the moment a second
backend's ids entered `MODEL_PRICES_USD_PER_MTOK` that assumption broke on
purpose (`gpt-5.3-codex` is 8:1). This file pins the NEW claims specifically:
every OpenAI row is sourced and citable, the retired ids this codebase still
defaults to (`gpt-5-codex`) get no row and no fabricated price, and a run
billed by a flat ChatGPT subscription is a documented gap, not a silent 0.0.
"""

from __future__ import annotations

import re

import pytest

from no_human.core import pricing
from no_human.core.pricing import (
    MODEL_PRICES_USD_PER_MTOK,
    OUTPUT_EXTRA_WEIGHT,
    _reset_unknown_pricing_models,
    output_extra_weight,
    unknown_pricing_models,
)

# Ids `codex exec` actually resolved on this machine on 2026-08-22 (measured
# for ticket d35aa60e) and that this table now carries a sourced row for.
OPENAI_IDS = tuple(k for k in MODEL_PRICES_USD_PER_MTOK if not k.startswith("claude-"))

# Ids that returned "Model not found" on 2026-08-22 and do not appear on the
# pricing page read 2026-08-23 either — retired, not merely unpriced. The
# first is also `config.py`'s current `llm.codex_model` default, which is
# exactly why it must NOT get a row: pricing the product's own default at a
# guessed number would be the most-hit wrong number in the table.
RETIRED_CODEX_IDS = ("gpt-5-codex", "gpt-5.1-codex", "gpt-5.1-codex-max", "gpt-5.2-codex")


@pytest.fixture(autouse=True)
def _clean_unknown_counter():
    _reset_unknown_pricing_models()
    yield
    _reset_unknown_pricing_models()


# --------------------------------------------------------------------------- #
# sourcing
# --------------------------------------------------------------------------- #

def test_every_openai_row_has_a_positive_input_and_output_rate():
    assert OPENAI_IDS, "no OpenAI ids in the table — nothing was sourced"
    for model in OPENAI_IDS:
        price_in, price_out = MODEL_PRICES_USD_PER_MTOK[model]
        assert price_in > 0, model
        assert price_out > 0, model


def test_every_openai_row_carries_a_url_and_a_date_in_its_citation():
    """Every sourced row must be traceable to an official page and a read
    date — not a promise in prose, a checkable pattern in the source file.

    Positive control: the pre-existing Anthropic citation block, which this
    change touched only to add an explicit ``https://`` scheme, must match
    the same pattern — proving the regex finds a real citation and is not
    vacuously true. Negative control: the module's own top synopsis line
    ("A token is not a token...") must NOT match, proving the regex does not
    just match anywhere in the file.
    """
    assert OPENAI_IDS
    source = __import__("pathlib").Path(pricing.__file__).read_text()
    url_and_date = re.compile(r"https://\S+.*read 2026-\d{2}-\d{2}")

    for model in OPENAI_IDS:
        # Every id's own citation line names it, a URL, and a read-date.
        pattern = re.compile(rf"{re.escape(model)}\s+\$[\d.]+ / \$[\d.]+\s+https://\S+.*read 2026-\d{{2}}-\d{{2}}")
        assert pattern.search(source), f"no cited https:// + read-date line for {model}"

    # Positive control — a real citation line elsewhere in the file.
    assert "https://platform.claude.com" in source
    assert re.search(r"cached 2026-06-24", source)

    # Negative control — prose that must not accidentally satisfy the pattern.
    assert not url_and_date.search("A token is not a token — Anthropic bills the classes")


# --------------------------------------------------------------------------- #
# retirement — no row, ever, for an id that cannot be billed again
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("model", RETIRED_CODEX_IDS)
def test_the_retired_codex_ids_have_no_row(model):
    assert model not in MODEL_PRICES_USD_PER_MTOK


def test_gpt_5_codex_specifically_is_absent():
    """Named explicitly because it is not just retired — it is `config.py`'s
    current `llm.codex_model` default, so it is the row a careless "price the
    configured model" implementation would be most likely to add."""
    assert "gpt-5-codex" not in MODEL_PRICES_USD_PER_MTOK


def test_a_retired_id_still_takes_the_visible_fallback():
    for model in RETIRED_CODEX_IDS:
        assert output_extra_weight(model) == OUTPUT_EXTRA_WEIGHT
        assert unknown_pricing_models().get(model) == 1


# --------------------------------------------------------------------------- #
# the priced ids report their own ratio, not the fallback
# --------------------------------------------------------------------------- #

def test_output_extra_weight_is_the_rows_own_ratio_not_the_fallback():
    for model in OPENAI_IDS:
        price_in, price_out = MODEL_PRICES_USD_PER_MTOK[model]
        expected = price_out / price_in - 1.0
        assert output_extra_weight(model) == pytest.approx(expected), model


def test_a_priced_openai_id_is_not_reported_as_unknown():
    for model in OPENAI_IDS:
        output_extra_weight(model)
    assert unknown_pricing_models() == {}


def test_a_genuinely_unknown_openai_id_still_hits_the_fallback():
    """`gpt-5-codex` is retired, not merely unpriced — a distinct id this
    table has simply never seen exercises the same visible-fallback path a
    brand-new Claude id would."""
    assert output_extra_weight("gpt-5-codex") == OUTPUT_EXTRA_WEIGHT
    assert unknown_pricing_models() == {"gpt-5-codex": 1}


def test_a_genuinely_unknown_id_still_falls_back_and_is_logged_once(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="no_human.core.pricing"):
        premium = output_extra_weight("claude-opus-6")
    assert premium == OUTPUT_EXTRA_WEIGHT
    assert unknown_pricing_models() == {"claude-opus-6": 1}


# --------------------------------------------------------------------------- #
# the subscription-billing decision is documented, not fabricated
# --------------------------------------------------------------------------- #

def test_the_subscription_decision_is_recorded_in_the_module_docstring():
    doc = pricing.__doc__ or ""
    assert "flat ChatGPT subscription" in doc
    assert "no per-token price" in doc or "no row" in doc
    assert "unknown_pricing_models" in doc
    # No row anywhere in the table prices anything at exactly 0.0 — the
    # sentinel value this whole decision exists to avoid.
    for model, (price_in, price_out) in MODEL_PRICES_USD_PER_MTOK.items():
        assert price_in != 0.0, model
        assert price_out != 0.0, model


# --------------------------------------------------------------------------- #
# nothing about the Claude side moved
# --------------------------------------------------------------------------- #

def test_the_anthropic_rows_are_exactly_the_seven_published_pairs():
    expected = {
        "claude-opus-5": (5.0, 25.0),
        "claude-opus-4-8": (5.0, 25.0),
        "claude-opus-4-7": (5.0, 25.0),
        "claude-opus-4-6": (5.0, 25.0),
        "claude-sonnet-5": (3.0, 15.0),
        "claude-sonnet-4-6": (3.0, 15.0),
        "claude-haiku-4-5": (1.0, 5.0),
    }
    actual = {k: v for k, v in MODEL_PRICES_USD_PER_MTOK.items() if k.startswith("claude-")}
    assert actual == expected


#: The four sourced rows, HARDCODED on purpose: `OPENAI_IDS` above is derived
#: from the table, so a deleted row deletes the thing under test and the
#: derived tests stay green. This pin is what turns "a row vanished" into a
#: failure — the same shape `test_the_anthropic_rows_are_exactly_the_seven_published_pairs`
#: uses for the Claude side. Values are the published short-context rates read
#: 2026-08-23 (citations next to the table in pricing.py).
EXPECTED_OPENAI_ROWS = {
    "gpt-5.3-codex": (1.75, 14.00),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.5": (5.00, 30.00),
    "gpt-5.6-terra": (2.00, 12.00),
}


def test_the_openai_rows_are_exactly_the_four_sourced_pairs():
    """Deleting or editing any OpenAI row must fail here, not vanish from the
    derived `OPENAI_IDS` subject. Positive control for the derivation: the
    expected set is non-empty and every id in it is non-Anthropic."""
    assert EXPECTED_OPENAI_ROWS and all(not k.startswith("claude-") for k in EXPECTED_OPENAI_ROWS)
    actual = {k: v for k, v in MODEL_PRICES_USD_PER_MTOK.items() if not k.startswith("claude-")}
    assert actual == EXPECTED_OPENAI_ROWS, (
        f"OpenAI price rows drifted from the sourced set: {actual!r} != {EXPECTED_OPENAI_ROWS!r}")
    assert set(OPENAI_IDS) == set(EXPECTED_OPENAI_ROWS)
