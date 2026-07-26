"""The review gate fails closed when no reviewer is wired (M0.3).

The reviewer is the only gate between an unreviewed diff and a PR. Returning a
passing decision when it is absent turns the hard gate into a silent rubber
stamp — CLAUDE.md #3. `nh watch` did exactly that in production.
"""

import ast
import pathlib

import pytest

from no_human.config import DEFAULT_CONFIG, load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.review.reviewer import ReviewerUnavailable

from .test_e2e_orchestrator import FakeBackend, _config, bare_repo, store  # noqa: F401


def _good_mutate(cwd):
    """A real, passing diff — so the task reaches the review gate rather than
    tripping the zero-diff breaker first."""
    (cwd / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
    )
    (cwd / "test_calc.py").write_text(
        "from calc import add, mul\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n"
    )


def test_default_config_fails_closed():
    assert DEFAULT_CONFIG["reviewer"]["allow_advisory"] is False


async def test_run_review_raises_when_no_reviewer_is_wired(store, tmp_path):
    cfg = _config(tmp_path)
    cfg.data["reviewer"]["allow_advisory"] = False
    orch = Orchestrator(store, cfg.data, FakeBackend(_good_mutate), SlackNotifier(None))
    with pytest.raises(ReviewerUnavailable, match="rubber stamp"):
        await orch._run_review(Task.new("t", repo_path="/r"), None, "attempt-1")


async def test_advisory_pass_through_requires_the_explicit_flag(store, tmp_path):
    """Opting in is allowed for eval/replay — but it is announced, never silent."""
    cfg = _config(tmp_path)
    cfg.data["reviewer"]["allow_advisory"] = True
    events = []
    orch = Orchestrator(
        store, cfg.data, FakeBackend(_good_mutate), SlackNotifier(None),
        event_sink=events.append,
    )
    decision = await orch._run_review(Task.new("t", repo_path="/r"), None, "attempt-1")

    assert decision.passed is True
    (advisory,) = [e for e in events if e["kind"] == "review_advisory"]
    assert "NOT reviewed" in advisory["text"]


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_a_missing_reviewer_escalates_instead_of_opening_a_pr(
    store, bare_repo, tmp_path
):
    """End to end: reverting the fail-closed guard lets this task reach
    AWAITING_APPROVAL with an unreviewed diff."""
    cfg = _config(tmp_path)
    cfg.data["reviewer"]["allow_advisory"] = False
    orch = Orchestrator(store, cfg.data, FakeBackend(_good_mutate), SlackNotifier(None))

    task = Task.new("add add()", repo_path=str(bare_repo))
    task.acceptance_criteria = ["add(a,b) returns a+b"]
    await store.create_task(task)
    outcome = await orch.run_task(task)

    assert outcome.status is not TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is None
    assert "no reviewer is configured" in outcome.detail


def test_no_production_orchestrator_is_built_without_a_reviewer():
    """`nh watch` built its own Orchestrator and forgot the reviewer, so it drove
    tasks to a PR with the gate pass-through. Nothing caught it: the constructor
    defaults `reviewer=None`. This is the guard that would have."""
    missing = []
    for path in pathlib.Path("src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "Orchestrator"
                and "reviewer" not in {kw.arg for kw in node.keywords}
            ):
                missing.append(f"{path}:{node.lineno}")
    assert not missing, (
        "production code must always wire the review gate; missing at: "
        + ", ".join(missing)
    )


# ------- the reviewer that reached no verdict (task 84251cb2, attempt 13) ---- #


class _FakeAgentResult:
    def __init__(self, final_text, *, is_error=False, stop_reason="end_turn"):
        self.final_text = final_text
        self.is_error = is_error
        self.stop_reason = stop_reason
        self.num_turns = 11
        self.tokens_used = 0
        self.cache_read_tokens = 0
        self.cache_creation_tokens = 0
        self.session_id = "s"


class _ReviewerBackend:
    """Replays a scripted sequence of reviewer sessions."""

    def __init__(self, *results):
        self._results = list(results)
        self.budgets: list[int] = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, on_event=None, **kw):
        self.budgets.append(max_turns)
        return self._results.pop(0)


# NB: the parser reads "items", not "checklist". This fixture originally used
# "checklist", so items parsed as [] — and the test only passed because of the
# vacuous-pass bug (empty checklist + passed:true ⇒ pass) the gate fix removed.
_VERDICT = (
    'REVIEW_JSON_START {"passed": true, "items": '
    '[{"label": "ok", "passed": true, "evidence": "calc.py:1"}]} REVIEW_JSON_END'
)
_MAX_TURNS_ERROR = "Claude Code returned an error result: Reached maximum number of turns (10)"


async def test_reviewer_out_of_turns_escalates_instead_of_blaming_the_coder(tmp_path):
    """Attempt 13 of task 84251cb2: the reviewer exhausted its own turn budget
    while reading files and never emitted REVIEW_JSON. The fail-closed decision
    was fed back as a coder finding ("reviewer produced no parseable
    REVIEW_JSON") and spent the task's last bounded attempt.

    Reverting `_agent_review`'s retry+raise makes this return passed=False,
    i.e. a finding the coder is then told to fix."""
    from no_human.review.reviewer import AdversarialReviewer

    backend = _ReviewerBackend(
        _FakeAgentResult(_MAX_TURNS_ERROR, is_error=True, stop_reason="max_turns"),
        _FakeAgentResult(_MAX_TURNS_ERROR, is_error=True, stop_reason="max_turns"),
    )
    reviewer = AdversarialReviewer(backend=backend)

    with pytest.raises(ReviewerUnavailable, match="no verdict"):
        await reviewer._agent_review("prompt", tmp_path)

    assert len(backend.budgets) == 2, "one bounded infra retry (constraint #4)"
    assert backend.budgets[1] > backend.budgets[0], "the retry gets more turns"


async def test_a_reviewer_that_recovers_on_the_retry_returns_its_verdict(tmp_path):
    from no_human.review.reviewer import AdversarialReviewer

    backend = _ReviewerBackend(
        _FakeAgentResult(_MAX_TURNS_ERROR, is_error=True, stop_reason="max_turns"),
        _FakeAgentResult(_VERDICT),
    )
    decision = await AdversarialReviewer(backend=backend)._agent_review("p", tmp_path)
    assert decision.passed is True


async def test_review_timeout_halves_the_retry_window_on_a_hang(tmp_path, monkeypatch):
    """A hung/saturated reviewer (timeout, not turn exhaustion) must not be
    granted a second FULL window — a 50-line diff sat 20min in review (2×600s)
    in prod. The retry's window is halved; the task escalates ~5min sooner."""
    import asyncio
    import time as _time

    import no_human.review.reviewer as rv

    # Small floor so the shrink is observable in a fast test.
    monkeypatch.setattr(rv, "_REVIEW_MIN_RETRY_TIMEOUT", 0.05)

    windows: list[float] = []

    class _HangingBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, on_event=None, **kw):
            start = _time.monotonic()
            try:
                await asyncio.sleep(5)  # never finishes inside the wait_for window
            except asyncio.CancelledError:
                windows.append(_time.monotonic() - start)
                raise
            return _FakeAgentResult(_VERDICT)

    with pytest.raises(ReviewerUnavailable, match="no verdict"):
        await rv.AdversarialReviewer(backend=_HangingBackend())._agent_review(
            "p", tmp_path, timeout=0.4
        )

    assert len(windows) == 2, "one bounded infra retry (constraint #4)"
    # Round 2's window was ~half of round 1's — not another full 0.4s.
    assert windows[1] < windows[0] * 0.75


async def test_a_real_failing_verdict_is_never_retried_or_swallowed(tmp_path):
    """A genuine FAIL is a finding, not an infra error: return it on round one."""
    from no_human.review.reviewer import AdversarialReviewer

    # "items", not "checklist" — with the wrong key this asserted False for the
    # wrong reason (empty checklist fails closed) instead of the failing finding.
    fail = (
        'REVIEW_JSON_START {"passed": false, "items": '
        '[{"label": "bug", "passed": false, "severity": "high", '
        '"evidence": "calc.py:3"}]} REVIEW_JSON_END'
    )
    backend = _ReviewerBackend(_FakeAgentResult(fail))
    decision = await AdversarialReviewer(backend=backend)._agent_review("p", tmp_path)
    assert decision.passed is False
    assert len(backend.budgets) == 1, "a real finding must not trigger an infra retry"


async def test_reviewer_turn_budget_outgrew_the_pre_D16_default():
    """The 10-turn cap predates the reviewer being able to read files."""
    from no_human.review import reviewer as r
    assert r._REVIEW_TURNS >= 30


async def test_run_review_does_not_convert_no_verdict_into_a_coder_finding(
    store, tmp_path, bare_repo
):
    """`_run_review`'s `except Exception` used to swallow ReviewerUnavailable and
    return a failing ReviewDecision — re-poisoning the coder's feedback."""
    from no_human.core.orchestrator import Orchestrator

    class _DeadReviewer:
        model = "claude-opus-5"
        _on_event = None
        async def review(self, *a, **kw):
            raise ReviewerUnavailable("reviewer reached no verdict")

    cfg = _config(tmp_path)
    orch = Orchestrator(
        store, cfg.data, FakeBackend(_good_mutate), SlackNotifier(None),
        reviewer=_DeadReviewer(),
    )
    task = Task.new("t", repo_path=str(bare_repo))
    await store.create_task(task)
    with pytest.raises(ReviewerUnavailable):
        await orch._run_review(task, orch._open_repo(task), "attempt-1")
