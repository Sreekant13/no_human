"""Backend-level guards for the Claude Agent SDK wrapper.

Regression for the shadow-validation finding (2026-06-22): the SDK signals
hitting max_turns by *raising* a bare Exception from inside query(), not by
emitting a ResultMessage. The backend must convert that into a normal is_error
result so the orchestrator's bounded loop treats it as a failed attempt instead
of crashing the whole process.
"""

import pytest

# The whole module exercises the REAL ClaudeBackend over a monkeypatched SDK
# seam (claude_backend.query) — exempt from the hermetic stub by design.
pytestmark = pytest.mark.real_backend

from claude_agent_sdk import ResultMessage

from no_human.agent import claude_backend
from no_human.agent.claude_backend import ClaudeBackend


def _fake_query(error_message):
    async def _q(*args, **kwargs):
        raise Exception(error_message)
        yield  # pragma: no cover — makes this an async generator
    return _q


def _result_then_raise(result_msg, error_message):
    """Mimic the real SDK: emit a ResultMessage ('agent done'), THEN raise the
    terminal error — the exact sequence that crashed the orchestrator."""
    async def _q(*args, **kwargs):
        yield result_msg
        raise Exception(error_message)
    return _q


async def test_max_turns_exception_becomes_error_result(tmp_path, monkeypatch):
    monkeypatch.setattr(
        claude_backend, "query",
        _fake_query("Claude Code returned an error result: Reached maximum number of turns (40)"),
    )
    backend = ClaudeBackend(model="claude-opus-4-8")

    result = await backend.run("do it", cwd=tmp_path, max_turns=40)

    assert result.is_error is True
    assert result.stop_reason == "max_turns"
    assert result.num_turns == 40
    assert "maximum number of turns" in result.final_text.lower()


async def test_other_sdk_error_becomes_error_result(tmp_path, monkeypatch):
    monkeypatch.setattr(
        claude_backend, "query", _fake_query("transport blew up"),
    )
    backend = ClaudeBackend(model="claude-opus-4-8")

    result = await backend.run("do it", cwd=tmp_path, max_turns=40)

    # Any terminal SDK error is surfaced as a failed run, never raised.
    assert result.is_error is True
    assert result.stop_reason == "error"
    assert "transport blew up" in result.final_text


async def test_result_message_then_raise_is_corrected_to_error(tmp_path, monkeypatch):
    # The real-world crash: SDK emits a ResultMessage (is_error=False), then
    # raises the max-turns error. The corrective result event must win, so run()
    # returns is_error=True while preserving the real turn/token counts.
    rm = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=13, session_id="abc", result="agent done",
        usage={"input_tokens": 7000, "output_tokens": 347},
    )
    monkeypatch.setattr(
        claude_backend, "query",
        _result_then_raise(rm, "Claude Code returned an error result: Reached maximum number of turns (12)"),
    )
    backend = ClaudeBackend(model="claude-opus-4-8")

    result = await backend.run("do it", cwd=tmp_path, max_turns=12)

    assert result.is_error is True
    assert result.stop_reason == "max_turns"
    assert result.num_turns == 13           # preserved from the ResultMessage
    assert result.tokens_used == 7347       # preserved from the ResultMessage
    assert result.session_id == "abc"


async def test_stream_yields_a_result_event_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        claude_backend, "query", _fake_query("Reached maximum number of turns (40)"),
    )
    backend = ClaudeBackend(model="claude-opus-4-8")

    events = [e async for e in backend.stream("go", cwd=tmp_path, max_turns=40)]

    assert len(events) == 1
    assert events[0].kind == "result"
    assert events[0].meta["is_error"] is True
    assert events[0].meta["stop_reason"] == "max_turns"


async def test_genuine_error_preserves_traceback_for_diagnosis(tmp_path, monkeypatch):
    """A bare "'bool' object is not subscriptable" with no file:line burned 3
    attempts undiagnosably (task 6cfdb936). Genuine errors must carry the
    traceback so the crash is diagnosable."""
    monkeypatch.setattr(
        claude_backend, "query", _fake_query("'bool' object is not subscriptable"),
    )
    result = await ClaudeBackend(model="claude-opus-4-8").run(
        "do it", cwd=tmp_path, max_turns=40)
    assert result.is_error is True
    assert "'bool' object is not subscriptable" in result.final_text
    assert "Traceback" in result.final_text  # the diagnostic frames are preserved


async def test_max_turns_stays_clean_without_a_traceback(tmp_path, monkeypatch):
    """max_turns is not a crash — its message stays clean, no traceback noise."""
    monkeypatch.setattr(
        claude_backend, "query",
        _fake_query("Claude Code returned an error result: Reached maximum number of turns (40)"),
    )
    result = await ClaudeBackend(model="claude-opus-4-8").run(
        "x", cwd=tmp_path, max_turns=40)
    assert "Traceback" not in result.final_text


def test_thinking_is_a_dict_not_a_bool(tmp_path):
    """The SDK's `thinking` is a ThinkingConfig dict; passing True crashed every
    complex (thinking-enabled) task with 'bool' object is not subscriptable
    (task 6cfdb936)."""
    b = ClaudeBackend(model="claude-opus-4-8")
    opts = b._options(tmp_path, 40, thinking=True)
    assert isinstance(opts.thinking, dict) and opts.thinking["type"] == "adaptive"

    opts_budget = b._options(tmp_path, 40, thinking=True, max_thinking_tokens=8000)
    assert opts_budget.thinking == {"type": "enabled", "budget_tokens": 8000}

    opts_off = b._options(tmp_path, 40, thinking=False)
    assert not opts_off.thinking  # None / absent when not requested


def test_precompact_hook_registered_when_on_compact_set(tmp_path):
    """C1(a): compaction visibility. When the orchestrator passes on_compact,
    the SDK options must carry a PreCompact hook — the only way to KNOW whether
    the CLI's auto-compaction ever fires for coder sessions (it never has been
    observed; sessions end ~160k tokens, under the ~92% threshold)."""
    b = ClaudeBackend(model="claude-opus-4-8")
    opts = b._options(tmp_path, 40, on_compact=lambda trigger: None)
    assert "PreCompact" in opts.hooks

    opts_off = b._options(tmp_path, 40)
    assert "PreCompact" not in (opts_off.hooks or {})


async def test_precompact_hook_reports_trigger_and_allows():
    """The hook is pure telemetry: it forwards the trigger and never blocks."""
    calls = []
    hook = claude_backend._make_compact_hook(calls.append)
    out = await hook({"trigger": "auto"}, None, None)
    assert out == {}
    assert calls == ["auto"]


def test_coder_sessions_are_project_scoped(tmp_path):
    """C1-i2: passing skills= must pin setting_sources to ["project"] — the
    SDK's implicit default adds "user", which loads the operator's plugins
    (superpowers et al.), personal settings, and EVERY user skill into every
    coder session's context. Relevant user skills are delivered by copying
    them into the working tree instead (see _materialize_skills)."""
    b = ClaudeBackend(model="claude-opus-4-8")
    opts = b._options(tmp_path, 40, skills=["a", "b"])
    assert opts.setting_sources == ["project"]

    # A coder session with NO skills is still project-scoped — otherwise the
    # target repo's CLAUDE.md would not load for it (and consistency of the
    # prompt prefix across tasks would break).
    opts_bare = b._options(tmp_path, 40)
    assert opts_bare.setting_sources == ["project"]

    # Read-only sessions (reviewer/planner/supervisor) stay hermetic.
    r = ClaudeBackend(model="claude-opus-4-8", readonly=True)
    opts_ro = r._options(tmp_path, 40)
    assert opts_ro.setting_sources is None
