"""Per-edit lint feedback hook (IMPROVEMENT_PLAN B1)."""

import pytest

from no_human.agent.lint_hook import LintFeedbackHook
from no_human.testing import runner


def _hook(monkeypatch, *, ran, ok, output="E501 line too long"):
    def fake_lint(repo_path, lint_cmd, changed, *, timeout=60):
        return runner.LintResult(ran=ran, ok=ok, command=lint_cmd, output=output)
    monkeypatch.setattr(runner, "run_lint_on_changed", fake_lint)
    events = []
    h = LintFeedbackHook(
        repo_path="/repo", lint_cmd="ruff check",
        on_event=lambda k, t: events.append((k, t)),
    )
    return h, events


async def test_injects_on_lint_failure(monkeypatch):
    h, events = _hook(monkeypatch, ran=True, ok=False)
    out = await h.hook(
        {"tool_name": "Edit", "tool_input": {"file_path": "/repo/mod.py"}}, "id", None
    )
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "[LINT]" in ctx and "mod.py" in ctx and "E501" in ctx
    assert events and events[0][0] == "lint_feedback"


async def test_silent_when_lint_passes(monkeypatch):
    h, _ = _hook(monkeypatch, ran=True, ok=True)
    out = await h.hook(
        {"tool_name": "Write", "tool_input": {"file_path": "/repo/ok.py"}}, "id", None
    )
    assert out == {}


async def test_ignores_non_edit_tools(monkeypatch):
    h, _ = _hook(monkeypatch, ran=True, ok=False)
    out = await h.hook(
        {"tool_name": "Read", "tool_input": {"file_path": "/repo/mod.py"}}, "id", None
    )
    assert out == {}


async def test_ignores_non_code_files(monkeypatch):
    h, _ = _hook(monkeypatch, ran=True, ok=False)
    out = await h.hook(
        {"tool_name": "Edit", "tool_input": {"file_path": "/repo/README.md"}}, "id", None
    )
    assert out == {}


async def test_no_lint_cmd_is_noop(monkeypatch):
    # Even with a failing linter stubbed, no lint_cmd → never runs.
    monkeypatch.setattr(
        runner, "run_lint_on_changed",
        lambda *a, **k: runner.LintResult(True, False, "x", "boom"),
    )
    h = LintFeedbackHook(repo_path="/repo", lint_cmd=None)
    out = await h.hook(
        {"tool_name": "Edit", "tool_input": {"file_path": "/repo/mod.py"}}, "id", None
    )
    assert out == {}
