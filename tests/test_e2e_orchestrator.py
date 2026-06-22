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


# --------------------------------------------------------------------------- #
# Phase 5: agent-emitted structured blockers (Part 22)                        #
# --------------------------------------------------------------------------- #

class BlockerBackend:
    """A backend that emits a structured BLOCKER_JSON block instead of finishing.

    Models the agent hitting something it cannot solve without lowering the bar.
    Optionally mutates files first (to test that WIP is checkpointed).
    """

    def __init__(self, blocker_json: str, *, mutate=None):
        self._json = blocker_json
        self._mutate = mutate

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None):
        if self._mutate:
            self._mutate(cwd)
        text = (
            "I cannot proceed without lowering the bar.\n"
            "BLOCKER_JSON_START\n" + self._json + "\nBLOCKER_JSON_END\n"
        )
        return AgentResult(final_text=text, num_turns=1, is_error=False,
                           tokens_used=50, session_id="s", stop_reason="end_turn")


async def test_agent_ambiguity_blocker_routes_to_awaiting_input(bare_repo, tmp_path, store):
    """An AMBIGUITY blocker parks the task in awaiting_input with its question —
    never guesses, never fakes done (22.2)."""
    bjson = (
        '{"category": "AMBIGUITY", "confidence": 0.9, '
        '"root_cause_hypothesis": "criterion 2 contradicts criterion 1", '
        '"question": "Which behavior is correct for empty input?", '
        '"options": ["raise", "return 0"], '
        '"goal": "implement parse()", "evidence": "$ grep ...\\nno spec found"}'
    )
    cfg = _config(tmp_path)
    notes = []
    orch = Orchestrator(store, cfg.data, BlockerBackend(bjson), SlackNotifier(None),
                        event_sink=notes.append)
    t = Task.new("parse input", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_INPUT
    assert outcome.pr_url is None
    refreshed = await store.get_task(t.id)
    assert refreshed.status is TaskStatus.AWAITING_INPUT
    assert refreshed.blocker["category"] == "AMBIGUITY"
    assert refreshed.blocker["question"]
    assert "awaiting_input" in [e["kind"] for e in notes]


async def test_agent_impossible_blocker_escalates_not_faked(bare_repo, tmp_path, store):
    """The DoD red-team case: a deliberately-impossible task is escalated with
    evidence, never faked done."""
    bjson = (
        '{"category": "IMPOSSIBLE", "confidence": 0.95, '
        '"root_cause_hypothesis": "requested API does not exist in this version", '
        '"question": "This cannot be done as specified; drop or change it?", '
        '"goal": "call nonexistent API", "evidence": "ImportError: no such symbol"}'
    )
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, BlockerBackend(bjson), SlackNotifier(None))
    t = Task.new("impossible task", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    assert outcome.pr_url is None
    refreshed = await store.get_task(t.id)
    assert refreshed.blocker["category"] == "IMPOSSIBLE"


async def test_agent_dependency_wait_parks_and_checkpoints_wip(bare_repo, tmp_path, store):
    """A high-confidence DEPENDENCY_WAIT parks (blocked) with a wake condition and
    the partial work is checkpointed as [WIP-BLOCKED]."""
    def mutate(cwd):
        (cwd / "calc.py").write_text("def add(a, b):\n    return a + b  # WIP\n")

    bjson = (
        '{"category": "DEPENDENCY_WAIT", "confidence": 0.9, '
        '"wake_condition": "pr_merged:org/repo#42", '
        '"root_cause_hypothesis": "needs upstream PR #42 merged first", '
        '"goal": "use new upstream helper", "evidence": "import fails until #42 lands"}'
    )
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, BlockerBackend(bjson, mutate=mutate),
                        SlackNotifier(None))
    t = Task.new("use upstream helper", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.BLOCKED
    refreshed = await store.get_task(t.id)
    assert refreshed.blocker["wake_condition"] == "pr_merged:org/repo#42"
    assert refreshed.wake_check_at is not None  # watcher will re-evaluate
    # WIP was committed as [WIP-BLOCKED] on the feature branch.
    log = subprocess.run(["git", "log", "--all", "--oneline"], cwd=bare_repo,
                         capture_output=True, text=True).stdout
    assert "WIP-BLOCKED" in log or refreshed.blocker["resume_commit"]


async def test_low_confidence_dependency_wait_escalates(bare_repo, tmp_path, store):
    """Unsure-what's-wrong (confidence < threshold) escalates instead of parking
    silently (Part 22 config: escalate_on_low_confidence_below)."""
    bjson = (
        '{"category": "DEPENDENCY_WAIT", "confidence": 0.3, '
        '"wake_condition": "after:2h", '
        '"root_cause_hypothesis": "maybe a dependency? not sure", '
        '"question": "Unclear why this fails — advise?", '
        '"goal": "build", "evidence": "intermittent failure"}'
    )
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, BlockerBackend(bjson), SlackNotifier(None))
    t = Task.new("flaky build", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED


class PromptCapturingBackend:
    """First run emits an AMBIGUITY blocker; second run (after the human reply)
    records the prompt it received and applies a real fix."""

    def __init__(self, blocker_json, fix):
        self._json = blocker_json
        self._fix = fix
        self.calls = 0
        self.prompts: list[str] = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None, on_event=None):
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            text = "Need a decision.\nBLOCKER_JSON_START\n" + self._json + "\nBLOCKER_JSON_END\n"
            return AgentResult(final_text=text, num_turns=1, is_error=False,
                               tokens_used=30, session_id="s", stop_reason="end_turn")
        self._fix(cwd)
        return AgentResult(final_text="applied the agreed behavior", num_turns=2,
                           is_error=False, tokens_used=80, session_id="s",
                           stop_reason="end_turn")


async def test_reply_resumes_from_checkpoint_with_human_answer(bare_repo, tmp_path, store):
    """DoD: a parked task resumes from its checkpoint when a human replies, and
    the resumed (fresh) session is seeded with the human's answer."""
    bjson = (
        '{"category": "AMBIGUITY", "confidence": 0.9, '
        '"root_cause_hypothesis": "empty-input behavior unspecified", '
        '"question": "What should mul() do on empty input?", '
        '"options": ["raise", "return 0"], "goal": "implement mul", '
        '"evidence": "spec silent on empty input"}'
    )

    def fix(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    backend = PromptCapturingBackend(bjson, fix)
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns product"]
    await store.create_task(t)

    # 1. First run parks in awaiting_input with the question.
    outcome = await orch.run_task(t)
    assert outcome.status is TaskStatus.AWAITING_INPUT
    # base branch was captured as main and persisted (not the feature branch).
    parked = await store.get_task(t.id)
    assert parked.context["base_branch"] == "main"

    # 2. Simulate `nh reply <id> "return 0"`: store the answer, resume.
    refreshed = await store.get_task(t.id)
    ctx = refreshed.context or {}
    ctx["human_replies"] = [{"at": "2026-06-22", "question": "empty input?",
                             "answer": "return 0 on empty input"}]
    refreshed.context = ctx
    refreshed.wake_check_at = None
    await store.update_task(refreshed)
    await store.set_status(refreshed, TaskStatus.IMPLEMENTING, validate=False)

    # 3. Re-run: resumes from the checkpoint and completes to a PR.
    outcome2 = await orch.run_task(refreshed)
    assert outcome2.status is TaskStatus.AWAITING_APPROVAL
    assert outcome2.pr_url is not None

    # The resumed (fresh) session prompt carried the human's answer (22.5).
    resume_prompt = backend.prompts[-1]
    assert "return 0 on empty input" in resume_prompt
    assert "do NOT re-ask" in resume_prompt
    # Resume must re-base from main, not the parked feature branch.
    final = await store.get_task(t.id)
    assert final.context["base_branch"] == "main"


async def test_resume_after_wip_checkpoint_rebases_from_main(bare_repo, tmp_path, store):
    """A DEPENDENCY_WAIT parks with a [WIP-BLOCKED] commit on a feature branch.
    On resume the base must still be main — not the feature branch (which would
    make open_pr use base == head)."""
    bjson = (
        '{"category": "DEPENDENCY_WAIT", "confidence": 0.9, '
        '"wake_condition": "pr_merged:org/repo#42", '
        '"root_cause_hypothesis": "needs upstream PR", "goal": "use helper", '
        '"evidence": "import fails"}'
    )

    def wip(cwd):
        (cwd / "calc.py").write_text("def add(a, b):\n    return a + b  # partial WIP\n")

    def fix(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    backend = PromptCapturingBackend(bjson, fix)
    # First call mutates WIP then parks; override call 1 to also write WIP.
    backend._fix = fix  # used on call 2

    class _WipFirst:
        def __init__(self, inner, wip):
            self.inner = inner
            self.wip = wip
        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None, on_event=None):
            if self.inner.calls == 0:
                self.wip(cwd)
            return await self.inner.run(prompt, cwd=cwd, max_turns=max_turns,
                                        effort=effort, resume=resume, on_event=on_event)

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, _WipFirst(backend, wip), SlackNotifier(None))
    t = Task.new("use helper", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul works"]
    await store.create_task(t)

    o1 = await orch.run_task(t)
    assert o1.status is TaskStatus.BLOCKED
    parked = await store.get_task(t.id)
    assert parked.context["base_branch"] == "main"
    # WIP was checkpointed.
    log = subprocess.run(["git", "log", "--all", "--oneline"], cwd=bare_repo,
                         capture_output=True, text=True).stdout
    assert "WIP-BLOCKED" in log

    # Resume (simulate nh unblock → implementing) and complete.
    await store.set_status(parked, TaskStatus.IMPLEMENTING, validate=False)
    o2 = await orch.run_task(parked)
    assert o2.status is TaskStatus.AWAITING_APPROVAL
    final = await store.get_task(t.id)
    assert final.context["base_branch"] == "main"


# --------------------------------------------------------------------------- #
# Regression: agent hitting max_turns must escalate via the bounded loop,      #
# never crash the orchestrator (shadow-validation finding, 2026-06-22).        #
# --------------------------------------------------------------------------- #

class MaxTurnsBackend:
    """Backend that always returns a terminal max_turns error (as the real
    ClaudeBackend now does when the SDK raises 'maximum number of turns')."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None):
        self.calls += 1
        if on_event:
            on_event(AgentEvent("result", text="Reached maximum number of turns (40)"))
        return AgentResult(
            final_text="Reached maximum number of turns (40)",
            num_turns=max_turns, is_error=True, tokens_used=1234,
            session_id="s", stop_reason="max_turns",
        )


async def test_agent_max_turns_escalates_not_crashes(bare_repo, tmp_path, store):
    cfg = _config(tmp_path)
    backend = MaxTurnsBackend()
    events = []
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("do the hard thing", repo_path=str(bare_repo))
    await store.create_task(t)

    # Must NOT raise — the whole point of the fix.
    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    # The bounded loop ran every attempt, then escalated honestly.
    assert backend.calls == cfg.data["bounds"]["max_attempts"]
    attempts = await store.list_attempts(t.id)
    assert len(attempts) == cfg.data["bounds"]["max_attempts"]
    assert all(a["status"] == "failed" for a in attempts)
    assert all("max_turns" in (a.get("failure_reason") or "") for a in attempts)
    # No half-finished work was committed/pushed as an approvable PR. (A local
    # attempt branch may exist — it's created before the agent runs — but the
    # remote received no pushed branch.)
    assert outcome.pr_url is None
    remote_branches = subprocess.run(
        ["git", "ls-remote", "--heads", "origin"], cwd=bare_repo,
        capture_output=True, text=True).stdout
    assert "no-human/" not in remote_branches
    assert "agent_error" in [e["kind"] for e in events]
