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


# --------------------------------------------------------------------------- #
# B4: the utility tier — these two entry points hardcoded claude-opus-4-8      #
# --------------------------------------------------------------------------- #

import pytest

from no_human.config import DEFAULT_CONFIG
from no_human.intake.evaluator import evaluate_spec, resolve_assumptions


class _CapturingBackend:
    """Records the model it was constructed with; returns an empty verdict."""

    seen: list[str] = []

    def __init__(self, *, model, readonly=False, **_):
        _CapturingBackend.seen.append(model)

    async def run(self, prompt, **kwargs):
        from no_human.agent.claude_backend import AgentResult
        return AgentResult(final_text="", num_turns=1, is_error=False,
                           tokens_used=0, session_id="s", stop_reason="end_turn")


@pytest.fixture
def capture_backend(monkeypatch):
    _CapturingBackend.seen = []
    monkeypatch.setattr(
        "no_human.agent.claude_backend.ClaudeBackend", _CapturingBackend
    )
    return _CapturingBackend


@pytest.mark.asyncio
async def test_evaluate_spec_defaults_to_the_utility_model(capture_backend):
    """It used to construct ClaudeBackend(model="claude-opus-4-8") outright,
    so llm.* in config could never reach it."""
    await evaluate_spec("t", "d", [])
    assert capture_backend.seen == [DEFAULT_CONFIG["llm"]["utility_model"]]
    assert "opus" not in capture_backend.seen[0]


@pytest.mark.asyncio
async def test_resolve_assumptions_defaults_to_the_utility_model(capture_backend):
    await resolve_assumptions("t", "d", [])
    assert capture_backend.seen == [DEFAULT_CONFIG["llm"]["utility_model"]]


@pytest.mark.asyncio
async def test_an_explicit_model_wins_over_the_default(capture_backend):
    """Callers holding a resolved config pass model=; the default is only a floor."""
    await evaluate_spec("t", "d", [], model="claude-sonnet-5")
    assert capture_backend.seen == ["claude-sonnet-5"]
