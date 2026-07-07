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
                  on_event=None, supervisor_hook=None, **kwargs):
        if on_event:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "calc.py"}))
        self.mutate(cwd)
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    # Disable planning by default in tests — no real Claude calls.
    # Planning-specific tests override this and mock ClaudeBackend.
    cfg.data.setdefault("planning", {})["enabled"] = False
    return cfg


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


async def test_run_task_uses_confirmed_profile_test_command(bare_repo, tmp_path, store):
    """A usable ProjectProfile's proven test_cmd drives the run, not detect_command."""
    from no_human.profile import ProjectProfile

    marker = bare_repo / ".profile_ran"

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    # A profile whose test command writes a sentinel — distinct from `pytest -q`,
    # so its presence proves the profile (not the heuristic) chose the command.
    prof = ProjectProfile(
        repo_path=str(bare_repo), ecosystem="custom",
        test_cmd=f"sh -c 'echo ran > {marker}; exit 0'",
        derived_from=["test"], proven={"test_cmd": True}, confirmed=True,
    )
    await store.upsert_profile(prof)

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert marker.exists(), "profile test_cmd did not run"
    assert "profile" in [e["kind"] for e in events]


class _GatedCI:
    """A CI backend that must be started by a human (like JenkinsCI)."""
    name = "jenkins"
    max_infra_retries = 0

    async def trigger(self, branch, extra_variables=None):
        from no_human.ci.base import HumanGatedCI
        raise HumanGatedCI("build it first", wake_hint="Build image on Jenkins job X")


class _RecordingNotifier(SlackNotifier):
    def __init__(self):
        super().__init__(None)
        self.sent = []

    def notify(self, kind, message):
        self.sent.append((kind, message))


def _feature_mutate(cwd):
    (cwd / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
    (cwd / "test_calc.py").write_text(
        "from calc import add, mul\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n")


async def test_human_gated_ci_parks_with_wake_and_notifies(bare_repo, tmp_path, store):
    cfg = _config(tmp_path)
    notifier = _RecordingNotifier()
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(_feature_mutate), notifier,
                        event_sink=events.append, ci_runner=_GatedCI())
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.BLOCKED          # parked, not failed/infra
    t = await store.find_task(t.id)
    assert t.blocker["category"] == "DEPENDENCY_WAIT"
    assert t.blocker["wake_condition"].startswith("ci_green_on:no-human/")
    assert t.context["human_gated_ci"]["branch"].startswith("no-human/")
    # the human got a heads-up (parked-but-actionable), and the branch is pushed.
    assert notifier.sent and "Jenkins" in notifier.sent[-1][1]
    branches = subprocess.run(["git", "branch", "--list"], cwd=bare_repo,
                              capture_output=True, text=True).stdout
    assert "no-human/" in branches
    # no PR opened yet — CI hasn't verified the change.
    attempts = await store.list_attempts(t.id)
    assert all(not a.get("pr_url") for a in attempts)


async def test_human_gated_ci_resume_opens_pr_without_rerunning_agent(
    bare_repo, tmp_path, store
):
    cfg = _config(tmp_path)
    # First run: park on the gate.
    orch = Orchestrator(store, cfg.data, FakeBackend(_feature_mutate),
                        SlackNotifier(None), ci_runner=_GatedCI())
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)
    assert (await orch.run_task(t)).status is TaskStatus.BLOCKED

    # Human/watcher resumes (gate cleared). The agent must NOT run again, and the
    # gated CI must NOT be re-triggered — we go straight to the PR.
    class _Exploding:
        async def run(self, *a, **k):
            raise AssertionError("agent must not re-run on a human-gated resume")

    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
    t = await store.find_task(t.id)
    orch2 = Orchestrator(store, cfg.data, _Exploding(), SlackNotifier(None),
                         ci_runner=_GatedCI())

    outcome = await orch2.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url and "no-human/" in outcome.pr_url
    t = await store.find_task(t.id)
    assert "human_gated_ci" not in (t.context or {})
    assert t.blocker in (None, {})


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
                     before_ref="HEAD~1", after_ref="HEAD", **kwargs):
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

    # Escalated: stagnation detector fires after 2 identical failing attempts
    # (same review pass rate → agent is stuck), so only 2 reviewer calls.
    assert outcome.status is TaskStatus.ESCALATED
    assert outcome.pr_url is None
    assert len(call_count) == 2
    # Each attempt's review_passed is recorded as 0.
    attempts = await store.list_attempts(t.id)
    assert all(a["review_passed"] == 0 for a in attempts)


async def test_implement_prompt_uses_worktree_dir_not_primary_checkout(bare_repo, tmp_path, store):
    """Regression (validation found this): in concurrency mode the agent runs in a
    per-task worktree, so the prompt must point at that working dir — NOT
    task.repo_path (the primary checkout). Handing it the primary path made the
    agent edit the wrong tree; the worktree then showed 'no file changes'."""
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("add f()", repo_path="/primary/checkout")
    t.acceptance_criteria = ["f exists"]

    wt = "/tmp/worktrees/abc123"
    prompt = orch._build_implement_prompt(t, wt)
    assert wt in prompt
    assert "make ALL edits here" in prompt
    # The primary checkout must NOT be presented as the working directory.
    assert "repo at /primary/checkout" not in prompt

    # Backward-compat: with no work_dir it falls back to task.repo_path.
    assert "/primary/checkout" in orch._build_implement_prompt(t)


async def test_review_feedback_injected_into_next_attempt(bare_repo, tmp_path, store):
    """EVOLUTION_PLAN §2.2: on reviewer FAIL the cited findings are persisted and
    surface in the next attempt's implement prompt (no new loop — reuses the
    bounded attempt machinery; tamper guard still gates each round)."""
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("fix add()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["add(a,b) returns a+b"]
    await store.create_task(t)

    failed = [
        ChecklistItem("add(a,b) returns correct sum", False,
                      "calc.py:2 returns 0, not a+b", file="calc.py", line=2,
                      comment="Return a + b, not a hardcoded 0."),
    ]
    await orch._record_review_feedback(t, failed)

    refreshed = await store.get_task(t.id)
    assert refreshed.context["review_feedback"][0]["file"] == "calc.py"

    prompt = orch._build_implement_prompt(refreshed)
    assert "independent staff reviewer FAILED" in prompt
    assert "Return a + b, not a hardcoded 0." in prompt
    assert "calc.py:2" in prompt
    # The anti-tamper instruction must ride along with the feedback.
    assert "do NOT weaken" in prompt.lower() or "do not weaken" in prompt.lower()


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
                  on_event=None, supervisor_hook=None, **kwargs):
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

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None, on_event=None,
                  supervisor_hook=None, **kwargs):
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
        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None, on_event=None,
                      supervisor_hook=None, **kwargs):
            if self.inner.calls == 0:
                self.wip(cwd)
            return await self.inner.run(prompt, cwd=cwd, max_turns=max_turns,
                                        effort=effort, resume=resume, on_event=on_event,
                                        supervisor_hook=supervisor_hook)

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
                  on_event=None, supervisor_hook=None, **kwargs):
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


# --------------------------------------------------------------------------- #
# B5 regression: revision must reuse the existing PR branch                     #
# --------------------------------------------------------------------------- #

async def test_revision_reuses_pr_branch_b5(bare_repo, tmp_path, store):
    """B5: a revision (PR comment / nh reject) must push to the SAME branch the
    PR was opened on.  Before the fix the attempt loop restarted at attempt_n=1,
    computed a DIFFERENT branch name, and opened a duplicate PR."""
    call_count = 0

    def mutate(cwd):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            (cwd / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n\n"
                "def mul(a, b):\n    return a * b\n"
            )
            (cwd / "test_calc.py").write_text(
                "from calc import add, mul\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_mul():\n    assert mul(2, 3) == 6\n"
            )
        else:
            (cwd / "calc.py").write_text(
                "def add(a, b):\n    \"\"\"Add.\"\"\"\n    return a + b\n\n"
                "def mul(a, b):\n    \"\"\"Multiply.\"\"\"\n    return a * b\n"
            )
            (cwd / "test_calc.py").write_text(
                "from calc import add, mul\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_mul():\n    assert mul(2, 3) == 6\n"
            )

    cfg = _config(tmp_path)
    events: list[dict] = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    # --- Run 1: original work → PR opened ---
    outcome1 = await orch.run_task(t)
    assert outcome1.status is TaskStatus.AWAITING_APPROVAL
    t = await store.get_task(t.id)
    assert t.context.get("pr_branch"), "pr_branch must be stored on PR open"
    original_branch = t.context["pr_branch"]

    # --- Simulate PR comment → task resumed for revision ---
    ctx = t.context
    ctx["send_back_feedback"] = [{"at": "2026-06-28T12:00:00Z", "message": "add docstrings"}]
    t.context = ctx
    await store.update_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)

    # --- Run 2: revision → must reuse the same branch ---
    events.clear()
    outcome2 = await orch.run_task(t)
    assert outcome2.status is TaskStatus.AWAITING_APPROVAL

    t = await store.get_task(t.id)
    assert t.context["pr_branch"] == original_branch, (
        f"revision changed pr_branch from {original_branch!r} to "
        f"{t.context['pr_branch']!r} — B5 bug: duplicate PR"
    )
    # The attempt record must also show the original branch.
    attempts = await store.list_attempts(t.id)
    revision_attempt = attempts[-1]
    assert revision_attempt["branch_name"] == original_branch


# --------------------------------------------------------------------------- #
# Phase 1: plan-first worker                                                   #
#                                                                               #
# All tests mock ClaudeBackend — no real Claude API calls.  _config() disables  #
# planning by default; tests that exercise planning re-enable it explicitly.    #
# --------------------------------------------------------------------------- #

from unittest.mock import patch as _patch
from no_human.vcs import GitRepo


def _planning_config(tmp_path):
    """Config with planning explicitly enabled (for planning-specific tests)."""
    cfg = _config(tmp_path)
    cfg.data["planning"]["enabled"] = True
    return cfg


class PlannerBackend:
    """A backend that returns a scripted plan text (no real LLM)."""

    def __init__(self, plan_text: str):
        self._plan = plan_text

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        return AgentResult(final_text=self._plan, num_turns=3, is_error=False,
                           tokens_used=200, session_id="s", stop_reason="end_turn")


class FailingPlannerBackend:
    """A backend that always raises — simulates SDK auth failure."""

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        raise RuntimeError("no auth token")


_SAMPLE_PLAN = (
    "## FILES TO CHANGE/CREATE\n- calc.py: add mul()\n\n"
    "## APPROACH\nAdd a mul function.\n\n"
    "## TEST PLAN\ntest_mul asserts mul(2,3)==6.\n\n"
    "## OUT OF SCOPE\nDo not rename existing functions.\n\n"
    "## VERIFICATION\npytest -q\n"
)


async def test_planning_generates_and_stores_plan(bare_repo, tmp_path, store):
    """_generate_plan stores the plan text in task.context['plan']."""
    cfg = _planning_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]

    with _patch("no_human.core.orchestrator.ClaudeBackend",
                return_value=PlannerBackend(_SAMPLE_PLAN)):
        result = await orch._generate_plan(t, GitRepo(bare_repo))

    assert result == _SAMPLE_PLAN.strip()
    planning_events = [e for e in events if e.get("kind") == "planning"]
    assert any("plan generated" in e.get("text", "") for e in planning_events)


async def test_planning_skips_for_code_review(bare_repo, tmp_path, store):
    """Planning is gated: code_review kind skips it entirely (no Claude call)."""
    cfg = _planning_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("review PR #42", repo_path=str(bare_repo), kind="code_review")

    # No mock needed — code_review returns before creating a backend.
    result = await orch._generate_plan(t, GitRepo(bare_repo))

    assert result == ""
    planning_events = [e for e in events if e.get("kind") == "planning"]
    assert any("code_review" in e.get("text", "") for e in planning_events)


async def test_planning_skips_when_disabled(bare_repo, tmp_path, store):
    """Planning respects the config gate (no Claude call)."""
    cfg = _config(tmp_path)  # planning already disabled by _config()
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))

    # No mock needed — disabled returns before creating a backend.
    result = await orch._generate_plan(t, GitRepo(bare_repo))

    assert result == ""
    planning_events = [e for e in events if e.get("kind") == "planning"]
    assert any("disabled" in e.get("text", "") for e in planning_events)


async def test_planning_skip_plan_response(bare_repo, tmp_path, store):
    """When the planner assesses a trivial task, SKIP_PLAN bypasses planning."""
    cfg = _planning_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("fix typo", repo_path=str(bare_repo))

    with _patch("no_human.core.orchestrator.ClaudeBackend",
                return_value=PlannerBackend("SKIP_PLAN")):
        result = await orch._generate_plan(t, GitRepo(bare_repo))

    assert result == ""
    planning_events = [e for e in events if e.get("kind") == "planning"]
    assert any("trivial" in e.get("text", "") for e in planning_events)


async def test_planning_failure_is_best_effort(bare_repo, tmp_path, store):
    """Planning failure doesn't crash — returns empty string (mocked failure)."""
    cfg = _planning_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))

    with _patch("no_human.core.orchestrator.ClaudeBackend",
                return_value=FailingPlannerBackend()):
        result = await orch._generate_plan(t, GitRepo(bare_repo))

    assert result == ""
    planning_events = [e for e in events if e.get("kind") == "planning"]
    assert any("failed" in e.get("text", "") for e in planning_events)


async def test_plan_injected_into_implement_prompt(bare_repo, tmp_path, store):
    """Plan from task.context is injected into the implement prompt."""
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    t.context = {"plan": "## FILES TO CHANGE\n- calc.py: add mul()"}

    prompt = orch._build_implement_prompt(t)

    assert "IMPLEMENTATION PLAN" in prompt
    assert "calc.py: add mul()" in prompt
    assert "OUT OF SCOPE" in prompt  # the instruction about respecting scope


async def test_no_plan_no_plan_block_in_prompt(bare_repo, tmp_path, store):
    """Without a plan, the implement prompt has no plan block."""
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]

    prompt = orch._build_implement_prompt(t)

    assert "IMPLEMENTATION PLAN" not in prompt


async def test_full_pipeline_with_planning(bare_repo, tmp_path, store):
    """Full pipeline: planning (mocked) → implement (FakeBackend) → PR.

    This is the integration test that proves planning feeds into the implement
    prompt and the full lifecycle completes. All LLM calls are mocked.
    """
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

    cfg = _planning_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    with _patch("no_human.core.orchestrator.ClaudeBackend",
                return_value=PlannerBackend(_SAMPLE_PLAN)):
        outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    # Plan was stored in context
    refreshed = await store.get_task(t.id)
    assert refreshed.context.get("plan") == _SAMPLE_PLAN.strip()
    # Planning event was emitted
    kinds = [e["kind"] for e in events]
    assert "planning" in kinds
    # PLAN.md was written to worktree (but not committed)
    assert outcome.pr_url and "no-human/" in outcome.pr_url


# --------------------------------------------------------------------------- #
# Phase 7e: doom-loop detection wired through _agent_sink                      #
# --------------------------------------------------------------------------- #

class DoomLoopBackend:
    """Backend that emits 3 identical Read tool_use events (a doom-loop)."""

    def __init__(self, mutate):
        self.mutate = mutate

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        if on_event:
            for _ in range(3):
                on_event(AgentEvent("tool_use", tool_name="Read",
                                    tool_input={"file_path": "/src/foo.py"}))
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "calc.py"}))
        self.mutate(cwd)
        return AgentResult(final_text="done", num_turns=4, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


async def test_doom_loop_emits_stuck_event(bare_repo, tmp_path, store):
    """When the agent repeats the exact same tool call 3×, the orchestrator
    emits a 'stuck' event but does NOT interrupt the attempt (constraint #5)."""
    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef greet():\n    return 'hi'\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, greet\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_greet():\n    assert greet() == 'hi'\n"
        )

    cfg = _config(tmp_path)
    events: list[dict] = []
    orch = Orchestrator(store, cfg.data, DoomLoopBackend(mutate),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("trigger doom loop", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # The attempt should still complete — no mid-attempt interruption.
    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    # A "stuck" event with doom-loop text must have been emitted.
    stuck_events = [e for e in events if e.get("kind") == "stuck"]
    assert len(stuck_events) >= 1
    assert "doom-loop" in stuck_events[0]["text"]


# --------------------------------------------------------------------------- #
# Investigation tasks that produce findings but no code changes should         #
# complete as DONE with a report, not FAILED.                                  #
# --------------------------------------------------------------------------- #

class ReportOnlyBackend:
    """Backend that returns findings text but makes no file changes."""

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        return AgentResult(
            final_text="Root cause: the medstarhr instance stopped sending events "
                       "at 2026-07-04T18:00Z due to a misconfigured retention policy.",
            num_turns=5, is_error=False, tokens_used=500,
            session_id="s", stop_reason="end_turn",
        )


async def test_investigation_report_only_completes_as_done(bare_repo, tmp_path, store):
    """An investigation task that produces findings but no file changes → DONE."""
    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, ReportOnlyBackend(), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("investigate data drop", repo_path=str(bare_repo),
                 kind="investigation")
    t.acceptance_criteria = ["identify root cause of data drop"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.DONE, f"expected DONE, got {outcome.status}"
    assert "report-only" in outcome.detail
    # Findings stored in task context
    refreshed = await store.find_task(t.id)
    assert "findings" in (refreshed.context or {})
    assert "medstarhr" in refreshed.context["findings"]
    # Attempt marked succeeded, not failed
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["status"] == "succeeded"
    # investigation_report event emitted
    kinds = [e["kind"] for e in events]
    assert "investigation_report" in kinds


async def test_investigation_with_code_changes_follows_normal_path(bare_repo, tmp_path, store):
    """An investigation that also fixes the bug should follow the normal commit→PR flow."""
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

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None))
    t = Task.new("investigate and fix bug", repo_path=str(bare_repo),
                 kind="investigation")
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # Should follow normal PR flow, not the report-only path
    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url and "no-human/" in outcome.pr_url


async def test_non_investigation_no_changes_still_fails(bare_repo, tmp_path, store):
    """A feature task with no file changes should still FAIL (not get the report path)."""
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, ReportOnlyBackend(), SlackNotifier(None))
    t = Task.new("add feature", repo_path=str(bare_repo), kind="feature")
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # After exhausting max_attempts with no changes, the orchestrator escalates.
    assert outcome.status in (TaskStatus.FAILED, TaskStatus.ESCALATED)


# --------------------------------------------------------------------------- #
# env_setup / env_vars / env_teardown                                          #
# --------------------------------------------------------------------------- #

async def test_env_vars_injected_during_agent_run(bare_repo, tmp_path, store):
    """env_vars in task.config are visible to the agent and restored after."""
    import os
    sentinel_key = "_NH_TEST_ENV_SENTINEL"
    assert sentinel_key not in os.environ  # clean slate

    captured = {}

    class EnvCapturingBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                      on_event=None, supervisor_hook=None, **kwargs):
            captured["val"] = os.environ.get(sentinel_key)
            # Produce a file change so the task doesn't fail.
            (cwd / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
            (cwd / "test_calc.py").write_text(
                "from calc import add, mul\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_mul():\n    assert mul(2, 3) == 6\n")
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, EnvCapturingBackend(), SlackNotifier(None))
    t = Task.new("test env", repo_path=str(bare_repo))
    t.config = {"env_vars": {sentinel_key: "hello_nh"}}
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert captured["val"] == "hello_nh"
    # Cleaned up after
    assert sentinel_key not in os.environ


async def test_env_setup_failure_aborts_attempt(bare_repo, tmp_path, store):
    """A failing env_setup command should abort the attempt before the agent runs."""
    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("test setup fail", repo_path=str(bare_repo))
    t.config = {"env_setup": ["exit 1"]}
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # Should fail/escalate due to setup failure
    assert outcome.status in (TaskStatus.FAILED, TaskStatus.ESCALATED)
    kinds = [e["kind"] for e in events]
    assert "env_setup_failed" in kinds


# --------------------------------------------------------------------------- #
# Subagent materialization                                                     #
# --------------------------------------------------------------------------- #

async def test_subagents_materialized_before_agent_run(bare_repo, tmp_path, store):
    """Built-in subagent .md files are written to .claude/agents/ before the agent runs."""
    agents_dir_existed = {}

    class CheckingBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                      on_event=None, supervisor_hook=None, **kwargs):
            # Check that subagent files exist DURING the agent run.
            agents_dir = cwd / ".claude" / "agents"
            agents_dir_existed["exists"] = agents_dir.exists()
            agents_dir_existed["researcher"] = (agents_dir / "no_human_researcher.md").exists()
            # Produce file changes so the task completes.
            (cwd / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
            (cwd / "test_calc.py").write_text(
                "from calc import add, mul\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_mul():\n    assert mul(2, 3) == 6\n")
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, CheckingBackend(), SlackNotifier(None))
    t = Task.new("test subagents", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert agents_dir_existed.get("exists"), ".claude/agents/ dir not created"
    assert agents_dir_existed.get("researcher"), "no_human_researcher.md not materialized"

    # Verify the file content is valid YAML frontmatter + instructions.
    researcher_md = (bare_repo / ".claude" / "agents" / "no_human_researcher.md").read_text()
    assert "name: no_human_researcher" in researcher_md
    assert "NEVER edit files" in researcher_md


# --------------------------------------------------------------------------- #
# Verify skill materialization                                                 #
# --------------------------------------------------------------------------- #

async def test_verify_skill_materialized_with_test_cmd(bare_repo, tmp_path, store):
    """When a confirmed profile exists, a verify skill with the test_cmd is materialized."""
    from no_human.profile import ProjectProfile
    skill_found = {}

    class SkillCheckingBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                      on_event=None, supervisor_hook=None, **kwargs):
            skill_path = cwd / ".claude" / "skills" / "no_human_verify" / "SKILL.md"
            skill_found["exists"] = skill_path.exists()
            if skill_path.exists():
                skill_found["content"] = skill_path.read_text()
            (cwd / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
            (cwd / "test_calc.py").write_text(
                "from calc import add, mul\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_mul():\n    assert mul(2, 3) == 6\n")
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")

    marker = bare_repo / ".verify_skill_ran"
    prof = ProjectProfile(
        repo_path=str(bare_repo), ecosystem="custom",
        test_cmd=f"sh -c 'echo ran > {marker}; exit 0'",
        derived_from=["test"], proven={"test_cmd": True}, confirmed=True,
    )
    await store.upsert_profile(prof)

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, SkillCheckingBackend(), SlackNotifier(None))
    t = Task.new("test verify skill", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert skill_found.get("exists"), "verify skill not materialized"
    assert "echo ran" in skill_found["content"]
    assert "proven" in skill_found["content"].lower()


# --------------------------------------------------------------------------- #
# Compact instructions materialization                                         #
# --------------------------------------------------------------------------- #

async def test_compact_instructions_materialized(bare_repo, tmp_path, store):
    """Compact instructions (.claude/instructions.md) are written before the agent runs."""
    from no_human.profile import ProjectProfile
    instructions_found = {}

    class InstructionsCheckingBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                      on_event=None, supervisor_hook=None, **kwargs):
            inst_path = cwd / ".claude" / "instructions.md"
            instructions_found["exists"] = inst_path.exists()
            if inst_path.exists():
                instructions_found["content"] = inst_path.read_text()
            (cwd / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
            (cwd / "test_calc.py").write_text(
                "from calc import add, mul\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_mul():\n    assert mul(2, 3) == 6\n")
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")

    marker = bare_repo / ".inst_test_ran"
    prof = ProjectProfile(
        repo_path=str(bare_repo), ecosystem="python",
        test_cmd=f"sh -c 'echo ran > {marker}; exit 0'",
        derived_from=["test"], proven={"test_cmd": True}, confirmed=True,
    )
    await store.upsert_profile(prof)

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, InstructionsCheckingBackend(), SlackNotifier(None))
    t = Task.new("test instructions", repo_path=str(bare_repo), kind="investigation")
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert instructions_found.get("exists"), ".claude/instructions.md not created"
    content = instructions_found["content"]
    assert "python" in content.lower()
    assert "INVESTIGATION" in content.upper()


# --------------------------------------------------------------------------- #
# Intake evaluator for non-grill tasks (Phase 4)                               #
# --------------------------------------------------------------------------- #

async def test_intake_evaluator_runs_for_non_grill_tasks(
    bare_repo, tmp_path, store, monkeypatch,
):
    """Tasks without eval_result in context get intake evaluation during planning."""
    from no_human.intake.evaluator import EvalResult, EvalVerdict

    eval_called = {}

    async def fake_evaluate_spec(title, desc, criteria, *, backend=None):
        eval_called["yes"] = True
        return EvalResult(
            verdict=EvalVerdict.DECOMPOSE,
            dimensions={"bounded_scope": False},
            rationale="too large",
        )

    monkeypatch.setattr(
        "no_human.core.orchestrator.evaluate_spec", fake_evaluate_spec,
        raising=False,
    )
    # Also patch the import path used inside _drive.
    monkeypatch.setattr(
        "no_human.intake.evaluator.evaluate_spec", fake_evaluate_spec,
    )

    class SimpleBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                      on_event=None, supervisor_hook=None, **kwargs):
            (cwd / "calc.py").write_text("def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
            (cwd / "test_calc.py").write_text(
                "from calc import add, mul\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_mul():\n    assert mul(2, 3) == 6\n")
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")

    cfg = _config(tmp_path)
    cfg.data.setdefault("planning", {})["enabled"] = False
    orch = Orchestrator(store, cfg.data, SimpleBackend(), SlackNotifier(None))
    t = Task.new("big compound task", repo_path=str(bare_repo))
    await store.create_task(t)

    await orch.run_task(t)

    refreshed = await store.get_task(t.id)
    assert eval_called.get("yes"), "evaluate_spec was not called"
    assert refreshed.context.get("eval_result") is not None
    assert refreshed.context["eval_result"]["verdict"] == "decompose"


async def test_intake_evaluator_skipped_when_already_evaluated(
    bare_repo, tmp_path, store, monkeypatch,
):
    """Tasks that already have eval_result (grill path) skip re-evaluation."""
    eval_called = {}

    async def fake_evaluate_spec(title, desc, criteria, *, backend=None):
        eval_called["yes"] = True

    monkeypatch.setattr(
        "no_human.intake.evaluator.evaluate_spec", fake_evaluate_spec,
    )

    class SimpleBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                      on_event=None, supervisor_hook=None, **kwargs):
            (cwd / "calc.py").write_text("def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
            (cwd / "test_calc.py").write_text(
                "from calc import add, mul\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_mul():\n    assert mul(2, 3) == 6\n")
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")

    cfg = _config(tmp_path)
    cfg.data.setdefault("planning", {})["enabled"] = False
    orch = Orchestrator(store, cfg.data, SimpleBackend(), SlackNotifier(None))
    t = Task.new("already evaluated", repo_path=str(bare_repo))
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)

    await orch.run_task(t)

    assert not eval_called.get("yes"), "evaluate_spec should not be called again"


async def test_intake_evaluator_failure_does_not_block_pipeline(
    bare_repo, tmp_path, store, monkeypatch,
):
    """Evaluator failure is advisory — task proceeds normally."""
    async def failing_evaluate_spec(title, desc, criteria, *, backend=None):
        raise RuntimeError("evaluator crashed")

    monkeypatch.setattr(
        "no_human.intake.evaluator.evaluate_spec", failing_evaluate_spec,
    )

    class SimpleBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                      on_event=None, supervisor_hook=None, **kwargs):
            (cwd / "calc.py").write_text("def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
            (cwd / "test_calc.py").write_text(
                "from calc import add, mul\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_mul():\n    assert mul(2, 3) == 6\n")
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")

    cfg = _config(tmp_path)
    cfg.data.setdefault("planning", {})["enabled"] = False
    orch = Orchestrator(store, cfg.data, SimpleBackend(), SlackNotifier(None))
    t = Task.new("evaluator crash test", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)
    # Task proceeds past evaluator failure — not stuck.
    assert outcome is not None
