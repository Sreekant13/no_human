"""Pins for `core/model_catalog.py` — the pure model-picker logic (part 1 of
3; the endpoint/CLI and the Settings pane are separate tickets).

`test_defaults_equals_the_five_shipped_llm_defaults` is the tripwire: it
fails the moment anyone edits one of the five `DEFAULT_CONFIG["llm"]` model
defaults without updating this file, which is exactly the signal that a
change needs to go through review rather than slide in unnoticed.

Every rule in `validate` gets a positive AND a negative case, each named
after the rule it pins, so a failing test names what broke without anyone
reading a traceback.
"""

from __future__ import annotations

import inspect
import subprocess
import sys

import pytest

from no_human.agent.backend import CLAUDE_PINNED_ROLES
from no_human.core import model_catalog as mc
from no_human.core.pricing import MODEL_PRICES_USD_PER_MTOK


# --------------------------------------------------------------------------
# ROLES / PINNED_ROLES
# --------------------------------------------------------------------------


def test_roles_maps_all_five_user_facing_roles_to_their_config_keys():
    assert dict(mc.ROLES) == {
        "coder": "primary_model",
        "reviewer": "review_model",
        "planner": "planner_model",
        "supervisor": "supervisor_model",
        "utility": "utility_model",
    }
    from no_human.config import DEFAULT_CONFIG

    for config_key in mc.ROLES.values():
        assert config_key in DEFAULT_CONFIG["llm"]
    assert "intake" not in mc.ROLES
    assert "intake" not in mc.PINNED_ROLES


def test_pinned_roles_is_the_intersection_with_claude_pinned_roles():
    assert mc.PINNED_ROLES == frozenset(mc.ROLES) & set(CLAUDE_PINNED_ROLES)
    assert mc.PINNED_ROLES == {"reviewer", "planner", "supervisor", "utility"}
    assert "coder" not in mc.PINNED_ROLES


# --------------------------------------------------------------------------
# price classes derive from the pricing table by import
# --------------------------------------------------------------------------


def test_price_classes_derive_from_the_pricing_table_not_a_copy(monkeypatch):
    for role in mc.ROLES:
        for opt in mc.options_for(role):
            in_rate, out_rate = MODEL_PRICES_USD_PER_MTOK[opt.id]
            assert opt.price_class.input_rate == in_rate
            assert opt.price_class.output_rate == out_rate

    # Mutation guard: a copied table cannot see this new row appear.
    monkeypatch.setitem(
        MODEL_PRICES_USD_PER_MTOK, "claude-testonly-9", (7.0, 35.0)
    )
    options = mc.options_for("coder")
    injected = [o for o in options if o.id == "claude-testonly-9"]
    assert len(injected) == 1
    assert injected[0].price_class.label == "high"
    assert injected[0].price_class.input_rate == 7.0
    assert injected[0].price_class.output_rate == 35.0


def test_no_price_tuple_is_copied_outside_the_threshold_constants():
    source = inspect.getsource(mc)
    for in_rate, out_rate in MODEL_PRICES_USD_PER_MTOK.values():
        needle = f"({in_rate}, {out_rate})"
        assert needle not in source, (
            f"found a copied price tuple {needle} in model_catalog.py"
        )


# --------------------------------------------------------------------------
# options_for: exact offer set, default marking, ordering
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(mc.ROLES))
def test_options_for_offers_exactly_the_priced_claude_ids_filtered_by_vendor_pin(
    role,
):
    ids = {opt.id for opt in mc.options_for(role)}
    priced_claude_ids = {m for m in MODEL_PRICES_USD_PER_MTOK if m.startswith("claude-")}
    if role in mc.PINNED_ROLES:
        assert ids == priced_claude_ids
    else:
        assert ids == set(MODEL_PRICES_USD_PER_MTOK)


def test_a_priced_non_claude_id_is_offered_to_the_unpinned_coder_role_only(
    monkeypatch,
):
    # The vendor-pin filter must not be over-broad: a priced non-Claude id
    # is still offered to "coder" (not vendor-pinned) and excluded from
    # every pinned role.
    monkeypatch.setitem(MODEL_PRICES_USD_PER_MTOK, "gpt-5-codex", (2.5, 10.0))
    coder_ids = {opt.id for opt in mc.options_for("coder")}
    assert "gpt-5-codex" in coder_ids
    for role in mc.PINNED_ROLES:
        assert "gpt-5-codex" not in {opt.id for opt in mc.options_for(role)}


@pytest.mark.parametrize("role", list(mc.ROLES))
def test_exactly_one_option_is_default_and_matches_that_roles_config_default(
    role,
):
    options = mc.options_for(role)
    defaults_ = [o for o in options if o.is_default]
    assert len(defaults_) == 1
    assert defaults_[0].id == mc.defaults()[mc.ROLES[role]]


def test_options_are_sorted_by_price_then_id():
    options = mc.options_for("coder")
    keys = [(-o.price_class.input_rate, o.id) for o in options]
    assert keys == sorted(keys)


# --------------------------------------------------------------------------
# vendor pin note
# --------------------------------------------------------------------------


def test_the_vendor_pin_note_is_present_on_exactly_the_pinned_roles():
    for role in mc.ROLES:
        notes = {opt.note for opt in mc.options_for(role)}
        if role in mc.PINNED_ROLES:
            assert notes == {mc.VENDOR_PIN_NOTE}
            assert all(n != "" for n in notes)
        else:
            assert notes == {""}


def test_the_note_says_vendor_not_tier():
    note = mc.VENDOR_PIN_NOTE.lower()
    assert "vendor" in note or "backend" in note
    # It must frame this as a vendor pin, not a fixed/limited model tier.
    assert "not a limit on the model tier" in note
    assert "any claude model listed here is available" in note


# --------------------------------------------------------------------------
# vendor rule
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", sorted(mc.PINNED_ROLES))
def test_a_pinned_role_refuses_a_non_claude_id(role):
    reason = mc.validate(role, "gpt-5-codex", current={})
    assert reason is not None
    assert "not a Claude model" in reason


@pytest.mark.parametrize("role", sorted(mc.PINNED_ROLES))
def test_a_pinned_role_accepts_a_claude_id(role):
    default_id = mc.defaults()[mc.ROLES[role]]
    assert mc.validate(role, default_id, current={}) is None


def test_the_coder_is_not_vendor_pinned():
    # Same id, unpinned role: refused, but on PRICE, not vendor — the
    # ordering the module docstring says is intentional.
    reason = mc.validate("coder", "gpt-5-codex", current={})
    assert reason is not None
    assert "no published price" in reason
    assert "Claude" not in reason


def test_violates_vendor_pin_predicate_directly():
    assert mc._violates_vendor_pin("reviewer", "gpt-5-codex") is True
    assert mc._violates_vendor_pin("reviewer", "claude-sonnet-5") is False
    assert mc._violates_vendor_pin("coder", "gpt-5-codex") is False


# --------------------------------------------------------------------------
# review != coder, both directions
# --------------------------------------------------------------------------


def test_the_reviewer_may_not_take_the_coders_model():
    reason = mc.validate(
        "reviewer", "claude-sonnet-5", current={"primary_model": "claude-sonnet-5"}
    )
    assert reason is not None
    assert "different model" in reason


def test_the_coder_may_not_take_the_reviewers_model():
    reason = mc.validate(
        "coder", "claude-opus-4-8", current={"review_model": "claude-opus-4-8"}
    )
    assert reason is not None
    assert "different model" in reason


def test_a_distinct_reviewer_model_is_allowed():
    reason = mc.validate(
        "reviewer", "claude-opus-4-8", current={"primary_model": "claude-sonnet-5"}
    )
    assert reason is None


def test_the_collision_rule_passes_when_the_other_side_is_unset():
    assert mc.validate("reviewer", "claude-sonnet-5", current={}) is None
    assert (
        mc.validate(
            "reviewer", "claude-sonnet-5", current={"primary_model": ""}
        )
        is None
    )
    assert mc.validate("coder", "claude-opus-4-8", current={}) is None


def test_current_is_required_with_no_default():
    # `current` is keyword-only with no default: omitting it is a TypeError,
    # not a silently-skipped review gate. This is what closes the vector
    # where validate("reviewer", "claude-sonnet-5") (no `current` at all)
    # used to return None even though the shipped default primary_model IS
    # "claude-sonnet-5".
    with pytest.raises(TypeError):
        mc.validate("reviewer", "claude-sonnet-5")


def test_a_role_keyed_current_dict_raises_instead_of_silently_no_opping():
    # `current` must be keyed by config keys ("primary_model"/"review_model"),
    # never by role names ("coder"/"reviewer"). A role-keyed dict has neither
    # key `_collides_with_the_review_gate` reads, so it would otherwise
    # silently no-op the reviewer != coder gate.
    with pytest.raises(ValueError):
        mc.validate(
            "reviewer", "claude-sonnet-5", current={"coder": "claude-sonnet-5"}
        )


def test_the_collision_rule_actually_fires_against_the_shipped_primary_default():
    # RED-first: this is the exact scenario F2 named as skippable before the
    # fix — the shipped primary_model default IS "claude-sonnet-5".
    reason = mc.validate(
        "reviewer",
        "claude-sonnet-5",
        current={"primary_model": "claude-sonnet-5"},
    )
    assert reason is not None
    assert "different model" in reason


def test_collides_with_the_review_gate_predicate_directly():
    assert (
        mc._collides_with_the_review_gate(
            "reviewer", "claude-sonnet-5", {"primary_model": "claude-sonnet-5"}
        )
        is True
    )
    assert (
        mc._collides_with_the_review_gate(
            "coder", "claude-opus-4-8", {"review_model": "claude-opus-4-8"}
        )
        is True
    )
    assert mc._collides_with_the_review_gate("planner", "claude-opus-5", {}) is False
    assert (
        mc._collides_with_the_review_gate("reviewer", "claude-sonnet-5", {}) is False
    )


# --------------------------------------------------------------------------
# unknown id
# --------------------------------------------------------------------------


def test_an_unpriced_id_is_refused_with_the_price_reason():
    reason = mc.validate("coder", "totally-made-up-model", current={})
    assert reason is not None
    assert "no published price" in reason


def test_a_priced_id_is_allowed():
    assert mc.validate("coder", "claude-haiku-4-5", current={}) is None


def test_blank_and_none_model_ids_are_refused_not_crashed():
    assert mc.validate("coder", None, current={}) is not None
    assert mc.validate("coder", "", current={}) is not None
    assert mc.validate("coder", 12345, current={}) is not None


def test_has_no_published_price_predicate_directly():
    assert mc._has_no_published_price("nonexistent-id") is True
    assert mc._has_no_published_price("claude-sonnet-5") is False
    assert mc._has_no_published_price(None) is True
    assert mc._has_no_published_price("") is True


# --------------------------------------------------------------------------
# named predicates exist
# --------------------------------------------------------------------------


def test_each_rule_is_a_separately_named_predicate():
    assert callable(mc._violates_vendor_pin)
    assert callable(mc._has_no_published_price)
    assert callable(mc._collides_with_the_review_gate)

    assert mc._violates_vendor_pin("reviewer", "gpt-5-codex") is True
    assert mc._violates_vendor_pin("reviewer", "claude-opus-4-8") is False

    assert mc._has_no_published_price("bogus") is True
    assert mc._has_no_published_price("claude-opus-4-8") is False

    assert (
        mc._collides_with_the_review_gate(
            "coder", "claude-opus-4-8", {"review_model": "claude-opus-4-8"}
        )
        is True
    )
    assert mc._collides_with_the_review_gate("coder", "claude-sonnet-5", {}) is False


# --------------------------------------------------------------------------
# unknown role
# --------------------------------------------------------------------------


def test_an_unknown_role_raises_rather_than_returning_an_empty_list():
    with pytest.raises(ValueError):
        mc.options_for("nope")
    with pytest.raises(ValueError):
        mc.validate("nope", "claude-sonnet-5", current={})
    with pytest.raises(ValueError):
        mc.options_for("")
    with pytest.raises(ValueError):
        mc.options_for(None)


# --------------------------------------------------------------------------
# defaults()
# --------------------------------------------------------------------------


def test_defaults_equals_the_five_shipped_llm_defaults():
    expected = {
        "primary_model": "claude-sonnet-5",
        "review_model": "claude-opus-4-8",
        "planner_model": "claude-opus-5",
        "supervisor_model": "claude-sonnet-5",
        "utility_model": "claude-haiku-4-5",
    }
    assert mc.defaults() == expected

    from no_human.config import DEFAULT_CONFIG

    for config_key, value in expected.items():
        assert DEFAULT_CONFIG["llm"][config_key] == value


def test_defaults_returns_a_fresh_dict():
    a = mc.defaults()
    b = mc.defaults()
    assert a == b
    assert a is not b
    a["primary_model"] = "mutated"
    assert mc.defaults()["primary_model"] != "mutated"

    from no_human.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["llm"]["primary_model"] == "claude-sonnet-5"


# --------------------------------------------------------------------------
# shipped state is self-consistent
# --------------------------------------------------------------------------


def test_the_shipped_defaults_are_themselves_valid():
    d = mc.defaults()
    for role, config_key in mc.ROLES.items():
        reason = mc.validate(role, d[config_key], current=d)
        assert reason is None, f"{role} default {d[config_key]!r} rejected: {reason}"


# --------------------------------------------------------------------------
# purity: no web/CLI symbol
# --------------------------------------------------------------------------


def test_the_catalog_imports_no_web_or_cli_symbol():
    code = (
        "import sys; import no_human.core.model_catalog; "
        "print(','.join(sorted(m for m in ('fastapi', 'click', 'starlette', "
        "'uvicorn') if m in sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", (
        f"forbidden modules loaded: {result.stdout.strip()}"
    )


def test_source_has_no_import_of_a_forbidden_symbol():
    source = inspect.getsource(mc)
    lines = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]
    for line in lines:
        for token in ("fastapi", "click", "starlette", "uvicorn"):
            assert token not in line.lower(), f"forbidden import: {line!r}"
