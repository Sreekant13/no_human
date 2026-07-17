"""Suite-wide fixtures (EH1): no test eats a real production backoff.

The fast suite claimed ~50s and took ~12 minutes; part of that was real
sleeps leaking out of retry paths (a 30s PR-open retry pause, 120s CI infra
backoffs) whenever a test tripped them. Production delays are class/module
constants precisely so this file can zero them for every test — a test that
WANTS to observe a delay can set it back explicitly.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_real_backoffs(monkeypatch):
    from no_human.core.orchestrator import Orchestrator
    monkeypatch.setattr(Orchestrator, "PR_OPEN_RETRY_DELAY", 0)
    # CI infra backoffs (module constants, 120s each — CLAUDE.md's 2-minute
    # retry rule; the tests that exercise retries patch sleep themselves, but
    # one unpatched path used to cost 2 real minutes).
    import no_human.ci.gitlab as _gl
    import no_human.ci.jenkins as _jk
    monkeypatch.setattr(_gl, "_INFRA_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(_jk, "_INFRA_BACKOFF_SECONDS", 0)


class _HermeticUtilityBackend:
    """Stands in for every ClaudeBackend the ORCHESTRATOR constructs itself
    (utility eval, distillation, supervisor LLM, planners). Those calls are
    advisory by design — a junk answer degrades a hint, never a verdict — so
    an empty deterministic result is a legal outcome of each one.

    Why: the suite was spawning REAL claude-haiku subprocesses (found live
    during the 2026-07-17 overnight gate: chunk2 blocked minutes on one under
    subscription saturation). Real calls burn quota, are nondeterministic
    (live intake enrichment expanded acceptance_criteria mid-test), and hang
    the gate exactly when the bench saturates the subscription.
    """

    def __init__(self, *args, **kwargs):
        self.model = kwargs.get("model", "hermetic-stub")

    async def run(self, prompt, **kwargs):
        from no_human.agent.claude_backend import AgentResult
        return AgentResult(
            final_text="", num_turns=1, is_error=False, tokens_used=0,
            session_id="hermetic", stop_reason="end_turn",
        )


@pytest.fixture(autouse=True)
def _hermetic_sdk(request, monkeypatch):
    """No test reaches the real Claude API unless it says so explicitly
    (NH_TESTS_LIVE_SDK=1, or the `real_backend` marker for tests that
    exercise the REAL ClaudeBackend class over a mocked SDK client — e.g.
    the stream-accounting tests). Tests that exercise the stubbed paths
    inject their own fakes at closer seams (SupervisorHook(llm_call=...),
    reviewer backends, planner mocks) — this catches what nothing stubbed."""
    import os
    if os.environ.get("NH_TESTS_LIVE_SDK") == "1":
        yield
        return
    if request.node.get_closest_marker("real_backend"):
        yield
        return
    # THE SOURCE MODULE first: every lazy `from ..agent.claude_backend import
    # ClaudeBackend` executed at CALL time (intake/evaluator.py:121+187,
    # review/reviewer.py:916, api/app.py:63) resolves against this attribute.
    # Review of PR #105 (round 1) proved the orchestrator alias alone left the
    # intake evaluator LIVE: 33 real haiku subprocesses under a green suite.
    monkeypatch.setattr(
        "no_human.agent.claude_backend.ClaudeBackend", _HermeticUtilityBackend)
    # Names bound at IMPORT time don't follow the source module — patch each.
    monkeypatch.setattr(
        "no_human.core.orchestrator.ClaudeBackend", _HermeticUtilityBackend)
    monkeypatch.setattr(
        "no_human.cli.commands.ClaudeBackend", _HermeticUtilityBackend)
    yield
