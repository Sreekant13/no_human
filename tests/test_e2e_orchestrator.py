"""End-to-end orchestrator spine against a real bare repo, with a fake backend.

Proves the deterministic pipeline (branch -> commit -> tamper guard -> tests ->
push -> open local PR -> awaiting_approval) without spending LLM quota. A second
case proves the tamper guard blocks a test-weakening change and escalates.
"""

import subprocess

import pytest

from no_human.agent.claude_backend import AgentEvent, AgentResult
from no_human.config import load_config
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.review.reviewer import AdversarialReviewer, ReviewDecision
from no_human.review.selfcheck import ChecklistItem


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def bare_repo(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    # a product file + an existing test, so the tamper guard has a baseline
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (work / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


class FakeBackend:
    """Stands in for ClaudeBackend: applies a scripted file mutation."""

    def __init__(self, mutate):
        self.mutate = mutate

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None):
        if on_event:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "calc.py"}))
        self.mutate(cwd)
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


def _config(tmp_path):
    return load_config(tmp_path / "config.yaml")


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


async def test_full_pipeline_opens_local_pr(bare_repo, tmp_path, store):
    def mutate(cwd):
        # add a real feature + a real test (no tampering)
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n"
            "def mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url and "no-human/" in outcome.pr_url
    # branch pushed to the bare remote
    branches = subprocess.run(["git", "branch", "--list"], cwd=bare_repo,
                              capture_output=True, text=True).stdout
    assert "no-human/" in branches
    # attempt recorded with a PR + passing tests
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["pr_url"] == outcome.pr_url
    assert attempts[-1]["status"] == "succeeded"
    kinds = [e["kind"] for e in events]
    assert "pr_open" in kinds and "commit" in kinds


async def test_tamper_weakening_is_blocked_and_escalates(bare_repo, tmp_path, store):
    def mutate(cwd):
        # "fix" by gutting the existing test — the documented reward hack
        (cwd / "calc.py").write_text("def add(a, b):\n    return 0  # broken\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add\n\ndef test_add():\n    pass\n"  # assertion removed
        )

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None))
    t = Task.new("make tests green", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    assert "tamper" in outcome.detail.lower()
    refreshed = await store.get_task(t.id)
    assert refreshed.status is TaskStatus.ESCALATED
    assert refreshed.blocker is not None
    # nothing was pushed as an approvable PR
    assert outcome.pr_url is None


# --------------------------------------------------------------------------- #
# Phase 2: adversarial reviewer gate                                           #
# --------------------------------------------------------------------------- #

class FakeReviewer:
    """Injects a scripted ReviewDecision without running the LLM."""

    def __init__(self, decision: ReviewDecision, *, call_count: list | None = None):
        self._decision = decision
        self.calls: list[dict] = []
        self._call_count = call_count  # shared mutable list for multi-attempt tests

    async def review(self, task, *, repo_path, test_output="", held_out_output="",
                     before_ref="HEAD~1", after_ref="HEAD"):
        self.calls.append({"task_id": task.id})
        if self._call_count is not None:
            self._call_count.append(1)
        return self._decision


async def test_reviewer_passes_proceeds_to_pr(bare_repo, tmp_path, store):
    """Correct change + passing reviewer → AWAITING_APPROVAL."""
    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n"
            "def mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    passing_decision = ReviewDecision(
        passed=True,
        checklist=[
            ChecklistItem("mul(a,b) implemented", True, "calc.py:3 returns a*b"),
            ChecklistItem("tests added", True, "test_calc.py:5 test_mul asserts mul(2,3)==6"),
        ],
    )
    cfg = _config(tmp_path)
    reviewer = FakeReviewer(passing_decision)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append, reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns product"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is not None
    assert reviewer.calls  # reviewer was actually invoked
    # attempt records the review checklist
    attempts = await store.list_attempts(t.id)
    last = attempts[-1]
    assert last["review_passed"] == 1
    assert last["review_checklist"] is not None
    assert [e["kind"] for e in events].count("review") >= 1


async def test_reviewer_fails_blocks_pr_and_loops(bare_repo, tmp_path, store):
    """Flawed change + failing reviewer → reviewer blocks; after max_attempts → ESCALATED."""
    call_count: list = []

    def mutate(cwd):
        # Introduce a product file change without adequate tests
        (cwd / "calc.py").write_text("def add(a, b):\n    return 0  # broken impl\n")
        # No test changes — tamper guard stays clean, but reviewer catches the fault.

    failing_decision = ReviewDecision(
        passed=False,
        checklist=[
            ChecklistItem("add(a,b) returns correct sum", False,
                          "calc.py:2 returns 0, not a+b — implementation is wrong"),
            ChecklistItem("tests verify correctness", False,
                          "test_calc.py: existing test_add() would catch this; "
                          "tests were not updated to fail"),
        ],
    )
    cfg = _config(tmp_path)
    reviewer = FakeReviewer(failing_decision, call_count=call_count)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        reviewer=reviewer)
    t = Task.new("fix add()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["add(a,b) returns a+b"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # Escalated after max_attempts; never opened a PR.
    assert outcome.status is TaskStatus.ESCALATED
    assert outcome.pr_url is None
    # Reviewer was called once per attempt (max_attempts=3 by default).
    assert len(call_count) == 3
    # Each attempt's review_passed is recorded as 0.
    attempts = await store.list_attempts(t.id)
    assert all(a["review_passed"] == 0 for a in attempts)
    # Evidence from the reviewer surfaces in the escalation detail.
    assert "review failed" in outcome.detail.lower()


async def test_red_team_agent_weakens_test_tamper_guard_blocks(bare_repo, tmp_path, store):
    """Red-team: agent guts the test to make a broken impl pass.

    The tamper guard fires BEFORE the reviewer runs, so the reviewer never
    even gets called. Escalated with 'tamper' in the detail.
    """
    call_count: list = []

    def mutate(cwd):
        # Agent "fixes" broken impl by removing the assertion that catches it.
        (cwd / "calc.py").write_text("def add(a, b):\n    return 0  # still broken\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add\n\ndef test_add():\n    pass  # assertion deleted\n"
        )

    passing_decision = ReviewDecision(passed=True, checklist=[
        ChecklistItem("all good", True, "looks fine"),
    ])
    cfg = _config(tmp_path)
    reviewer = FakeReviewer(passing_decision, call_count=call_count)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        reviewer=reviewer)
    t = Task.new("make tests pass", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # Tamper guard fires; reviewer is NEVER called (guard is pre-review).
    assert outcome.status is TaskStatus.ESCALATED
    assert "tamper" in outcome.detail.lower()
    assert len(call_count) == 0, "reviewer must not run when tamper guard fires"
