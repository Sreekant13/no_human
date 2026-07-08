"""Regression tests for intake prompt rendering.

The evaluator/assumption prompts embed a literal JSON example. They were
rendered with str.format(), which parsed the JSON braces as replacement
fields and raised KeyError('"verdict"') / KeyError('"assumptions"') on every
call — silently disabling intake evaluation for all tasks.
"""

from no_human.intake.evaluator import (
    _ASSUMPTIONS_PROMPT,
    _EVAL_PROMPT,
    _render,
)


def test_render_substitutes_fields_and_preserves_json_braces():
    out = _render(_EVAL_PROMPT, title="My Task", description="desc",
                  criteria="  - c1")
    # Placeholders substituted.
    assert "My Task" in out
    assert "desc" in out
    assert "  - c1" in out
    # No unsubstituted placeholders remain.
    assert "{title}" not in out
    assert "{description}" not in out
    assert "{criteria}" not in out
    # The literal JSON example survives untouched (this is what str.format broke).
    assert '{"verdict": "accept|enrich|clarify|decompose",' in out


def test_render_does_not_raise_on_assumptions_prompt():
    out = _render(_ASSUMPTIONS_PROMPT, title="t", description="d",
                  criteria="  - c1")
    assert "t" in out and "d" in out
    assert '{"assumptions": ["...", "..."]}' in out


def test_str_format_would_have_raised_regression_guard():
    """Document the original failure so nobody reintroduces str.format here."""
    import pytest

    with pytest.raises(KeyError):
        _EVAL_PROMPT.format(title="t", description="d", criteria="c")
    with pytest.raises(KeyError):
        _ASSUMPTIONS_PROMPT.format(title="t", description="d", criteria="c")
