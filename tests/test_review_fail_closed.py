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
