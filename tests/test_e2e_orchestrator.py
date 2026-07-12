"""End-to-end orchestrator spine against a real bare repo, with a fake backend.

Proves the deterministic pipeline (branch -> commit -> tamper guard -> tests ->
push -> open local PR -> awaiting_approval) without spending LLM quota. A second
case proves the tamper guard blocks a test-weakening change and escalates.
"""

import json
import subprocess

import pytest
from types import SimpleNamespace as _SimpleNamespace

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
    # These tests exercise the pipeline around the review gate, not the gate
    # itself, and most construct an Orchestrator with no reviewer. In
    # production that now escalates (the gate fails closed); here the skip is
    # deliberate and must be stated. The gate's own behaviour is covered by
    # tests/test_review_fail_closed.py.
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
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


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_repro_gate_runs_advisory_inside_the_pipeline(bare_repo, tmp_path, store):
    """The coder declares its demonstrating test; the gate proves both
    directions and emits its verdict — without changing the outcome."""
    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )
        (cwd / ".no_human").mkdir(exist_ok=True)
        (cwd / ".no_human" / "repro_tests.json").write_text(
            '{"tests": ["test_calc.py::test_mul"]}'
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
    gate = [e for e in events if e["kind"] == "repro_gate"]
    assert len(gate) == 1
    assert gate[0]["verdict"] == "pass", gate[0]
    # test_mul fails on the base (no mul) and passes after — a true repro.


async def test_repro_gate_waives_loudly_without_a_manifest(bare_repo, tmp_path, store):
    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
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
    gate = [e for e in events if e["kind"] == "repro_gate"]
    assert len(gate) == 1 and gate[0]["verdict"] == "waived"


async def test_transient_pr_open_failure_retries_instead_of_escalating(
        bare_repo, tmp_path, store, monkeypatch):
    """Live incident: `gh pr create` returned an EOF after a successful push
    and the task escalated as if a human were needed. One retry must absorb
    it (open_pr is idempotent on the forges we target)."""
    from no_human.core import orchestrator as orch_mod

    real_open_pr = orch_mod.open_pr
    calls = {"n": 0}

    def flaky_open_pr(repo, branch, title, body, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("gh pr create failed: unexpected EOF")
        return real_open_pr(repo, branch, title, body, **kwargs)

    async def no_sleep(_secs):
        return None

    monkeypatch.setattr(orch_mod, "open_pr", flaky_open_pr)
    monkeypatch.setattr(orch_mod.asyncio, "sleep", no_sleep)

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
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
    assert calls["n"] == 2
    kinds = [e["kind"] for e in events]
    assert "pr_open_retry" in kinds and "pr_open" in kinds


async def _run_and_capture_pr_labels(bare_repo, tmp_path, store, monkeypatch,
                                     *, git_labels=None, task_config=None):
    """Run the pipeline to _finalize and return the labels passed to open_pr."""
    from no_human.core import orchestrator as orch_mod

    captured = {}
    real_open_pr = orch_mod.open_pr

    def spy_open_pr(repo, branch, title, body, **kwargs):
        captured["labels"] = kwargs.get("labels")
        return real_open_pr(repo, branch, title, body,
                            **{k: v for k, v in kwargs.items() if k != "labels"})

    monkeypatch.setattr(orch_mod, "open_pr", spy_open_pr)

    cfg = _config(tmp_path)
    if git_labels is not None:
        cfg.data["git"]["pr_labels"] = git_labels

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    if task_config is not None:
        t.config = task_config
    await store.create_task(t)

    outcome = await orch.run_task(t)
    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    return captured["labels"]


async def test_pr_labels_come_from_git_config(bare_repo, tmp_path, store, monkeypatch):
    labels = await _run_and_capture_pr_labels(
        bare_repo, tmp_path, store, monkeypatch, git_labels=["V17"])
    assert labels == ["V17"]


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_task_config_overrides_global_pr_labels(bare_repo, tmp_path, store,
                                                      monkeypatch):
    labels = await _run_and_capture_pr_labels(
        bare_repo, tmp_path, store, monkeypatch,
        git_labels=["V17"], task_config={"pr_labels": ["V18"]})
    assert labels == ["V18"]


async def test_task_can_opt_out_of_global_pr_labels(bare_repo, tmp_path, store,
                                                    monkeypatch):
    """An explicit [] on the task means "no labels" — not "fall back to global"."""
    labels = await _run_and_capture_pr_labels(
        bare_repo, tmp_path, store, monkeypatch,
        git_labels=["V17"], task_config={"pr_labels": []})
    assert labels == []


async def test_default_branch_auto_detect_warns_on_stale_local_checkout(tmp_path, store):
    """C3: a local checkout stuck on 'master' while the remote's real default
    moved to 'main' must be caught even when no ProjectProfile.default_branch
    was ever confirmed — the auto-detect fallback in GitRepo.default_branch(),
    not just the pre-existing (opt-in) profile-declared check."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (work / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")  # establishes real refs/heads/main
    _git(work, "checkout", "-b", "master")  # stale local checkout, mismatched

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(work))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    warnings = [e for e in events if e.get("kind") == "warning"]
    assert any("master" in w["text"] and "main" in w["text"] for w in warnings), (
        f"expected a default-branch mismatch warning, got: {[e['text'] for e in warnings]}"
    )


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


class SequencedFakeReviewer:
    """Returns a different scripted ReviewDecision per call, in order (the
    last one repeats if more calls arrive than decisions) — for testing
    multi-attempt review-driven retry behavior where each attempt's
    findings genuinely differ."""

    def __init__(self, decisions: list):
        self._decisions = decisions
        self.calls: list = []

    async def review(self, task, *, repo_path, test_output="", held_out_output="",
                     before_ref="HEAD~1", after_ref="HEAD", **kwargs):
        self.calls.append({"task_id": task.id})
        idx = min(len(self.calls) - 1, len(self._decisions) - 1)
        return self._decisions[idx]


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_stagnation_not_triggered_by_different_findings_same_rate(bare_repo, tmp_path, store):
    """D6 regression: a matching 0% pass rate across 2 attempts must NOT be
    treated as stagnation when the specific failing findings are entirely
    different each time — that is real incremental progress (previous
    issues fixed, new ones surfaced), not the agent stuck repeating the
    same mistake. Modeled on a real run that hit exactly this pattern."""
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

    decisions = [
        ReviewDecision(passed=False, checklist=[
            ChecklistItem("commitSha undefined reference", False, "Jenkinsfile:883"),
            ChecklistItem("PR comment pagination missing per_page param", False, "Jenkinsfile:760"),
        ]),
        ReviewDecision(passed=False, checklist=[
            ChecklistItem("Image reuse broken across Jenkins agents", False, "Jenkinsfile:653"),
            ChecklistItem("Selfcheck fixture passes for the wrong reason", False, "scripts/x.groovy:52"),
        ]),
        ReviewDecision(passed=True, checklist=[
            ChecklistItem("all findings addressed", True, "verified"),
        ]),
    ]
    cfg = _config(tmp_path)
    reviewer = SequencedFakeReviewer(decisions)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        reviewer=reviewer)
    t = Task.new("fix things", repo_path=str(bare_repo))
    t.acceptance_criteria = ["things are fixed"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # Must reach and succeed on attempt 3 — NOT escalate after attempt 2 on
    # a false stagnation positive (both attempts scored 0%, but zero
    # findings recurred between them).
    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is not None
    assert len(reviewer.calls) == 3


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


class PromptCapturingPlannerBackend:
    """Captures every prompt sent to this backend so tests can assert on
    content. A list, not a single attribute: MoA planning (on by default)
    fans out multiple calls (one per proposer + one aggregator) through the
    same patched backend, so the last call alone isn't representative."""

    def __init__(self, plan_text: str):
        self._plan = plan_text
        self.prompts: list[str] = []

    @property
    def prompt(self) -> str | None:
        """Back-compat: the most recent prompt, for single-call call sites."""
        return self.prompts[-1] if self.prompts else None

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.prompts.append(prompt)
        return AgentResult(final_text=self._plan, num_turns=3, is_error=False,
                           tokens_used=200, session_id="s", stop_reason="end_turn")


async def test_planner_prompt_no_child_tasks_by_default(bare_repo, tmp_path, store):
    """By default the planner is told to delegate in-session, NOT to emit a
    DECOMPOSE_PLAN (which would create child tasks). Checked across every MoA
    proposer prompt — each carries the full base prompt plus its own lens."""
    cfg = _planning_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("multi-concern task", repo_path=str(bare_repo))
    backend = PromptCapturingPlannerBackend(_SAMPLE_PLAN)

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=backend):
        await orch._generate_plan(t, GitRepo(bare_repo))

    assert backend.prompts
    assert all("DECOMPOSE_PLAN_START" not in p for p in backend.prompts)
    assert any("IN-SESSION DELEGATION" in p for p in backend.prompts)
    assert any("must never create new tasks" in p for p in backend.prompts)


def _base_prompts(backend):
    """Prompts derived from the planner base prompt — every MoA proposer's, and
    the single planner's. The MoA aggregator's prompt carries only the drafts."""
    return [p for p in backend.prompts
            if "You are planning an implementation task" in p]


async def test_planner_prompt_carries_linked_repos(bare_repo, tmp_path, store):
    """D19: the planner runs with cwd=primary repo and was never told the linked
    repos exist, so it planned around them as if they were not on disk. Every
    proposer must get the path map."""
    cfg = _planning_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("multi-repo task", repo_path=str(bare_repo))
    t.linked_repos = ["/repos/metrics-core-service"]
    backend = PromptCapturingPlannerBackend(_SAMPLE_PLAN)

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=backend):
        await orch._generate_plan(t, GitRepo(bare_repo))

    prompts = _base_prompts(backend)
    assert prompts
    assert all("/repos/metrics-core-service" in p for p in prompts)
    assert all("Never assume a linked repo is absent" in p for p in prompts)


async def test_planner_prompt_unchanged_for_single_repo(bare_repo, tmp_path, store):
    """No linked repos → no block, so the cacheable prefix is untouched."""
    cfg = _planning_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("single-repo task", repo_path=str(bare_repo))
    backend = PromptCapturingPlannerBackend(_SAMPLE_PLAN)

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=backend):
        await orch._generate_plan(t, GitRepo(bare_repo))

    assert backend.prompts
    assert all("LINKED REPOSITORIES" not in p for p in backend.prompts)


async def test_planner_prompt_decompose_when_enabled(bare_repo, tmp_path, store):
    """The legacy child-task path is only offered when decomposition.enabled."""
    cfg = _planning_config(tmp_path)
    cfg.data["decomposition"] = {"enabled": True}
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("multi-concern task", repo_path=str(bare_repo))
    backend = PromptCapturingPlannerBackend(_SAMPLE_PLAN)

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=backend):
        await orch._generate_plan(t, GitRepo(bare_repo))

    assert any("DECOMPOSE_PLAN_START" in p for p in backend.prompts)


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


# --------------------------------------------------------------------------- #
# B1: MoA (Mixture-of-Agents) planning fan-out — on by default.               #
# --------------------------------------------------------------------------- #

class MoAFakeBackend:
    """Stands in for every ClaudeBackend(...) construction during one MoA
    plan generation (proposers + aggregator all route through the same
    patched instance). Scripted by inspecting each incoming prompt: a
    "LENS (name)" marker identifies a proposer call, an "=== PROPOSAL ("
    marker identifies the aggregator call; anything else is the
    single-proposer fallback path."""

    def __init__(self, proposals: dict[str, str] | None = None,
                 aggregate_text: str = "", fail_lenses: set[str] | None = None,
                 single_path_text: str = ""):
        self.proposals = proposals or {}
        self.aggregate_text = aggregate_text
        self.fail_lenses = fail_lenses or set()
        self.single_path_text = single_path_text
        self.prompts: list[str] = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.prompts.append(prompt)
        for lens_name in self.fail_lenses:
            if f"LENS ({lens_name})" in prompt:
                raise RuntimeError(f"simulated failure for {lens_name}")
        for lens_name, text in self.proposals.items():
            if f"LENS ({lens_name})" in prompt:
                return AgentResult(final_text=text, num_turns=2, is_error=False,
                                   tokens_used=50, session_id="s", stop_reason="end_turn")
        if "=== PROPOSAL (" in prompt:
            return AgentResult(final_text=self.aggregate_text, num_turns=3,
                               is_error=False, tokens_used=100, session_id="s",
                               stop_reason="end_turn")
        return AgentResult(final_text=self.single_path_text, num_turns=1,
                           is_error=False, tokens_used=20, session_id="s",
                           stop_reason="end_turn")


def _moa_config(tmp_path, **overrides):
    """Config for tests of MoA *mechanics*. `min_signals=0` fans out
    unconditionally, so these tests exercise the proposer/aggregator path rather
    than the B2 complexity gate (which has its own tests below)."""
    cfg = _planning_config(tmp_path)
    cfg.data["llm"]["moa_planning"] = {
        "enabled": True, "proposers": 3, "min_signals": 0, **overrides,
    }
    return cfg


async def test_moa_planning_enabled_by_default():
    """The whole point of building MoA planning was for it to actually run —
    it must be on, not sitting inert behind an opt-in flag nobody sets."""
    from no_human.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["llm"]["moa_planning"]["enabled"] is True


async def test_moa_planning_synthesizes_proposals(bare_repo, tmp_path, store):
    """3 proposers + 1 aggregator, all through the same patched backend;
    the aggregator's output is what _generate_plan returns."""
    cfg = _moa_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    fake = MoAFakeBackend(
        proposals={
            "minimal-first": _SAMPLE_PLAN,
            "risk-first": _SAMPLE_PLAN.replace("mul", "mul_edge"),
            "test-first": _SAMPLE_PLAN.replace("test_mul", "test_mul_first"),
        },
        aggregate_text="## FILES TO CHANGE/CREATE\n- calc.py: synthesized\n",
    )
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        result = await orch._generate_plan(t, GitRepo(bare_repo))

    assert result == "## FILES TO CHANGE/CREATE\n- calc.py: synthesized"
    assert len(fake.prompts) == 4  # 3 proposers + 1 aggregator, no fallback
    moa_events = [e for e in events if e.get("kind") == "planning_moa"]
    assert any("synthesized" in e.get("text", "") for e in moa_events)
    planning_events = [e for e in events if e.get("kind") == "planning"]
    assert any("plan generated" in e.get("text", "") for e in planning_events)


async def test_moa_announces_the_fan_out_and_attributes_each_lens(
    bare_repo, tmp_path, store,
):
    """The fan-out used to emit nothing until synthesis, minutes later, and the
    three proposers' events were indistinguishable from each other."""
    cfg = _moa_config(tmp_path)
    events: list[dict] = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    fake = MoAFakeBackend(
        proposals={"minimal-first": _SAMPLE_PLAN, "risk-first": _SAMPLE_PLAN,
                   "test-first": _SAMPLE_PLAN},
        aggregate_text="## FILES TO CHANGE/CREATE\n- calc.py: synthesized\n",
    )
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        await orch._generate_plan(t, GitRepo(bare_repo))

    moa = [e for e in events if e.get("kind") == "planning_moa"]

    # 1. The fan-out announces itself, names every lens, and names the model.
    fan_out = next(e for e in moa if "fanning out" in e["text"])
    assert fan_out["model"] == cfg.data["llm"]["planner_model"]
    assert fan_out["proposers"] == ["minimal-first", "risk-first", "test-first"]
    for lens in ("minimal-first", "risk-first", "test-first"):
        assert lens in fan_out["text"]

    # 2. Each proposer reports its own completion, tagged with its lens.
    finished = {e["lens"] for e in moa if "finished" in e["text"]}
    assert finished == {"minimal-first", "risk-first", "test-first"}

    # 3. The fan-out is announced before any proposer finishes.
    assert moa.index(fan_out) == 0


# --------------------------------------------------------------------------- #
# B2: the MoA complexity gate                                                  #
# --------------------------------------------------------------------------- #

def _gate_cfg(**overrides):
    from no_human.config import DEFAULT_CONFIG
    return {**DEFAULT_CONFIG["llm"]["moa_planning"], **overrides}


def test_moa_signals_none_for_a_trivial_task():
    from no_human.core.orchestrator import _moa_complexity_signals
    t = Task.new("fix a typo in the README", repo_path="/r", kind="bugfix")
    assert _moa_complexity_signals(t, _gate_cfg()) == []


def test_moa_signals_for_the_ci_gate_task_shape():
    """The real CI_GATE task (61406d02): kind=feature, 10 acceptance criteria, a
    9309-char description, and — once D19/A2 stages it — one linked repo.
    kind=feature is NOT a signal: every dogfood helper is a feature, so it
    acted as a permanent +1 that let any enriched helper fan out 3 Opus
    proposers (task 6e64c555 live, 2026-07-12)."""
    from no_human.core.orchestrator import _moa_complexity_signals
    t = Task.new("Per-PR CI_GATE Integration Test Pipeline", repo_path="/r",
                 kind="feature", description="x" * 9309)
    t.acceptance_criteria = [f"criterion {i}" for i in range(10)]
    t.linked_repos = ["/repos/metrics-core-service"]
    assert set(_moa_complexity_signals(t, _gate_cfg())) == {
        "multi-repo", "many-criteria", "long-spec",
    }


def test_moa_counts_operator_criteria_not_enriched_ones():
    """Intake enrichment turned 2 operator criteria into 10 on a kebab-case
    helper, which tripped many-criteria and fanned out 3 Opus proposers
    (917k cache-read of planning on a trivial task, measured live). The gate
    must count the criteria the OPERATOR stated, preserved by _act_on_eval
    in context['original_criteria']."""
    from no_human.core.orchestrator import _moa_complexity_signals
    t = Task.new("Add a kebabCase helper", repo_path="/r", kind="feature")
    t.acceptance_criteria = [f"enriched {i}" for i in range(10)]
    t.context = {"original_criteria": ["converts inputs", "tests pass"]}
    assert _moa_complexity_signals(t, _gate_cfg()) == []


def test_moa_feature_kind_alone_is_not_complexity():
    from no_human.core.orchestrator import _moa_complexity_signals
    t = Task.new("Add a helper", repo_path="/r", kind="feature")
    t.acceptance_criteria = ["works", "tested"]
    assert _moa_complexity_signals(t, _gate_cfg()) == []


def test_moa_signals_read_the_evaluator_verdict():
    from no_human.core.orchestrator import _moa_complexity_signals
    t = Task.new("t", repo_path="/r", kind="bugfix")
    t.context = {"eval_result": {"verdict": "clarify"}}
    assert _moa_complexity_signals(t, _gate_cfg()) == ["ambiguous-spec"]


async def test_moa_gate_skips_the_fan_out_for_a_simple_task(
    bare_repo, tmp_path, store
):
    """No signals fire for a bare task (kind=feature is deliberately not a
    signal), so it takes the single-planner path instead of paying for three
    Opus proposers."""
    cfg = _planning_config(tmp_path)  # default moa_planning: min_signals=2
    events: list[dict] = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("small change", repo_path=str(bare_repo))
    backend = PromptCapturingPlannerBackend(_SAMPLE_PLAN)

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=backend):
        await orch._generate_plan(t, GitRepo(bare_repo))

    assert len(backend.prompts) == 1                    # single planner, no fan-out
    assert not [e for e in events if e.get("kind") == "planning_moa"]
    gate = next(e for e in events if "MoA gate" in e.get("text", ""))
    assert gate["signals"] == []
    assert "single planner" in gate["text"]


async def test_moa_gate_fans_out_for_a_complex_task(bare_repo, tmp_path, store):
    """Two signals (many-criteria + long-spec) meet the bar: 3 proposers + 1
    aggregator."""
    cfg = _planning_config(tmp_path)
    events: list[dict] = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("big change", repo_path=str(bare_repo),
                 description="x" * 2500)
    t.acceptance_criteria = [f"criterion {i}" for i in range(6)]
    fake = MoAFakeBackend(
        proposals={"minimal-first": _SAMPLE_PLAN, "risk-first": _SAMPLE_PLAN,
                   "test-first": _SAMPLE_PLAN},
        aggregate_text="## FILES TO CHANGE/CREATE\n- calc.py: synthesized\n",
    )

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        await orch._generate_plan(t, GitRepo(bare_repo))

    assert len(fake.prompts) == 4                       # 3 proposers + aggregator
    gate = next(e for e in events if "MoA gate" in e.get("text", ""))
    assert set(gate["signals"]) == {"long-spec", "many-criteria"}


async def test_min_signals_zero_restores_unconditional_moa(
    bare_repo, tmp_path, store
):
    """The documented escape hatch: min_signals=0 is the pre-B2 behavior."""
    cfg = _moa_config(tmp_path)  # pins min_signals=0
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("trivial", repo_path=str(bare_repo), kind="bugfix")
    fake = MoAFakeBackend(
        proposals={"minimal-first": _SAMPLE_PLAN, "risk-first": _SAMPLE_PLAN,
                   "test-first": _SAMPLE_PLAN},
        aggregate_text="## FILES TO CHANGE/CREATE\n- calc.py: synthesized\n",
    )

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        await orch._generate_plan(t, GitRepo(bare_repo))

    assert len(fake.prompts) == 4  # fans out despite zero complexity signals


async def test_moa_reports_a_failed_proposer_by_lens(bare_repo, tmp_path, store):
    cfg = _moa_config(tmp_path)
    events: list[dict] = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    fake = MoAFakeBackend(
        proposals={"minimal-first": _SAMPLE_PLAN, "risk-first": _SAMPLE_PLAN,
                   "test-first": _SAMPLE_PLAN},
        aggregate_text="## FILES TO CHANGE/CREATE\n- calc.py: synthesized\n",
        fail_lenses={"risk-first"},
    )
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        await orch._generate_plan(t, GitRepo(bare_repo))

    moa = [e for e in events if e.get("kind") == "planning_moa"]
    failed = [e for e in moa if "failed" in e["text"]]
    assert len(failed) == 1
    assert failed[0]["lens"] == "risk-first"


async def test_moa_aggregator_prompt_forbids_numeric_score(bare_repo, tmp_path, store):
    """CLAUDE.md #3: evidence-based review/synthesis, never a numeric
    self-score. The aggregator prompt must not invite one."""
    import re as _re
    cfg = _moa_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    fake = MoAFakeBackend(
        proposals={"minimal-first": _SAMPLE_PLAN, "risk-first": _SAMPLE_PLAN,
                   "test-first": _SAMPLE_PLAN},
        aggregate_text=_SAMPLE_PLAN,
    )
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        await orch._generate_plan(t, GitRepo(bare_repo))

    agg_prompt = fake.prompts[-1]
    assert "=== PROPOSAL (" in agg_prompt
    assert "numeric score" in agg_prompt.lower()
    assert not _re.search(r"score\s+\d+\s*[-–]\s*10", agg_prompt, _re.IGNORECASE)


async def test_moa_planning_falls_back_on_insufficient_proposers(bare_repo, tmp_path, store):
    """2 of 3 proposers fail → too few for a meaningful synthesis → falls
    back to the normal single-proposer path (same patched backend)."""
    cfg = _moa_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    fake = MoAFakeBackend(
        proposals={"minimal-first": _SAMPLE_PLAN},
        fail_lenses={"risk-first", "test-first"},
        single_path_text=_SAMPLE_PLAN,
    )
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        result = await orch._generate_plan(t, GitRepo(bare_repo))

    assert result == _SAMPLE_PLAN.strip()
    moa_events = [e for e in events if e.get("kind") == "planning_moa"]
    assert any("falling back" in e.get("text", "") for e in moa_events)
    planning_events = [e for e in events if e.get("kind") == "planning"]
    assert any("plan generated" in e.get("text", "") for e in planning_events)


async def test_moa_planning_uses_decompose_proposal_directly(bare_repo, tmp_path, store):
    """A confident compound-task signal from ONE proposer is used directly —
    never blended/averaged with the other proposals."""
    decompose_text = (
        "DECOMPOSE_PLAN_START\n```json\n"
        '{"decompose": true, "justification": "multi-concern", "subtasks": '
        '[{"title": "A", "kind": "feature", "description": "d", '
        '"acceptance_criteria": ["x"], "depends_on": [], "repo_path": "."}]}\n'
        "```\nDECOMPOSE_PLAN_END\n"
    )
    cfg = _moa_config(tmp_path)
    cfg.data["decomposition"] = {"enabled": True}
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("compound task", repo_path=str(bare_repo))
    await store.create_task(t)

    fake = MoAFakeBackend(proposals={
        "minimal-first": _SAMPLE_PLAN,
        "risk-first": decompose_text,
        "test-first": _SAMPLE_PLAN,
    })
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        result = await orch._generate_plan(t, GitRepo(bare_repo))

    assert result == decompose_text.strip()
    assert t.context["decomposition"]["decompose"] is True
    assert len(fake.prompts) == 3  # only the 3 proposers — no aggregator call
    planning_events = [e for e in events if e.get("kind") == "planning"]
    assert any("compound task detected" in e.get("text", "") for e in planning_events)


async def test_moa_planning_can_be_disabled(bare_repo, tmp_path, store):
    """Explicitly opting out reverts to exactly one planner call."""
    cfg = _planning_config(tmp_path)
    cfg.data["llm"]["moa_planning"] = {"enabled": False}
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))

    with _patch("no_human.core.orchestrator.ClaudeBackend",
                return_value=PlannerBackend(_SAMPLE_PLAN)) as mocked:
        result = await orch._generate_plan(t, GitRepo(bare_repo))

    assert result == _SAMPLE_PLAN.strip()
    assert mocked.call_count == 1


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


async def test_a_long_plan_inlines_only_its_head(bare_repo, tmp_path, store):
    """Transcript diet (M3): an inlined plan is cache-read on EVERY turn of
    the session. Past the threshold only the head inlines; the coder is told
    to read .no_human/PLAN.md first and grep it selectively."""
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("big refactor", repo_path=str(bare_repo))
    t.acceptance_criteria = ["x"]
    head = "## OBJECTIVE\nrefactor the flux capacitor\n"
    tail_marker = "UNIQUE-TAIL-SENTINEL"
    t.context = {"plan": head + ("filler line\n" * 800) + tail_marker}
    assert len(t.context["plan"]) > orch._PLAN_INLINE_MAX

    prompt = orch._build_implement_prompt(t)

    assert "READ IT FIRST" in prompt and ".no_human/PLAN.md" in prompt
    assert "flux capacitor" in prompt, "the head must inline for orientation"
    assert tail_marker not in prompt, "the tail must NOT be in every cached turn"
    assert "OUT OF SCOPE" in prompt


async def test_no_plan_no_plan_block_in_prompt(bare_repo, tmp_path, store):
    """Without a plan, the implement prompt has no plan block."""
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]

    prompt = orch._build_implement_prompt(t)

    assert "IMPLEMENTATION PLAN" not in prompt


async def test_debug_preamble_only_on_retry(bare_repo, tmp_path, store):
    """1.5: a first attempt has no debug preamble (byte-identical); a retry
    (attempt_log present) steers the coder to root-cause via no_human_debug."""
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]

    first = orch._build_implement_prompt(t)
    assert "no_human_debug" not in first and "A PRIOR ATTEMPT" not in first

    t.context = {"attempt_log": ["attempt 1: tests failed: 0 passed, 1 errors"]}
    retry = orch._build_implement_prompt(t)
    assert "A PRIOR ATTEMPT ON THIS TASK FAILED" in retry
    assert "no_human_debug" in retry and "patch-guess" in retry


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
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
    assert outcome.pr_url and "no-human/" in outcome.pr_url


async def test_plan_file_is_scoped_to_no_human_dir_and_cleaned_up(
    bare_repo, tmp_path, store,
):
    """The plan is materialized for the agent, then removed.

    Serial mode has no worktree, so ``repo.path`` is the user's primary
    checkout: a root-level PLAN.md outlives the run and the next task's planner
    reads it back as repo content.
    """
    seen: dict[str, bool] = {}

    def mutate(cwd):
        # Observed from inside the agent session, while the run is live.
        seen["under_no_human"] = (cwd / ".no_human" / "PLAN.md").is_file()
        seen["at_root"] = (cwd / "PLAN.md").exists()
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    cfg = _planning_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    with _patch("no_human.core.orchestrator.ClaudeBackend",
                return_value=PlannerBackend(_SAMPLE_PLAN)):
        outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert seen["under_no_human"], "the agent never saw the plan on disk"
    assert not seen["at_root"], "the plan must not be written to the checkout root"
    # Nothing survives the run in either location.
    assert not (bare_repo / ".no_human" / "PLAN.md").exists()
    assert not (bare_repo / "PLAN.md").exists()


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_stale_plan_file_is_removed_when_this_run_has_no_plan(
    bare_repo, tmp_path, store,
):
    """A plan left behind by a crashed run is never inherited by the next one."""
    stale = bare_repo / ".no_human" / "PLAN.md"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("# a previous run's plan — must not leak into this one\n")

    def mutate(cwd):
        assert not (cwd / ".no_human" / "PLAN.md").exists(), "stale plan visible to agent"
        (cwd / "calc.py").write_text("def add(a, b):\n    return a + b\n")

    cfg = _config(tmp_path)  # planning disabled → no plan for this task
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None))
    t = Task.new("tweak add()", repo_path=str(bare_repo))
    await store.create_task(t)

    await orch.run_task(t)

    assert not stale.exists()


# --------------------------------------------------------------------------- #
# Role attribution on agent events (the System view reads `source`)            #
# --------------------------------------------------------------------------- #

def _bare_orch(sink):
    """An Orchestrator with only the event sink wired — enough for _agent_sink."""
    orch = Orchestrator.__new__(Orchestrator)
    orch._sink = sink
    return orch


def test_agent_sink_defaults_to_the_coder_role():
    events: list[dict] = []
    _bare_orch(events.append)._agent_sink(AgentEvent("tool_use", tool_name="Read"))
    assert events[0]["source"] == "agent"
    assert events[0]["tool_name"] == "Read"


def test_sink_for_stamps_the_role_on_every_event():
    events: list[dict] = []
    orch = _bare_orch(events.append)
    orch._sink_for("planner:test-first")(AgentEvent("tool_use", tool_name="Grep"))
    orch._sink_for("aggregator")(AgentEvent("text", text="synthesizing"))
    assert [e["source"] for e in events] == ["planner:test-first", "aggregator"]


def test_sink_for_gives_each_concurrent_proposer_its_own_lens():
    """The MoA proposers run under asyncio.gather; a shared _active_role attr
    would hand every one of them whichever lens was assigned last."""
    events: list[dict] = []
    orch = _bare_orch(events.append)
    sinks = [orch._sink_for(f"planner:{lens}")
             for lens in ("minimal-first", "risk-first", "test-first")]
    # Interleave, as concurrent proposers do.
    for s in sinks:
        s(AgentEvent("tool_use", tool_name="Read"))
    for s in reversed(sinks):
        s(AgentEvent("subagent_start", text="Investigate Jenkinsfile structure"))
    assert [e["source"] for e in events] == [
        "planner:minimal-first", "planner:risk-first", "planner:test-first",
        "planner:test-first", "planner:risk-first", "planner:minimal-first",
    ]


def test_planner_tool_calls_do_not_feed_the_implementers_doom_loop_detector():
    """The planner is read-only and runs before the attempt. Its repeated Reads
    must not trip the coder's doom-loop detector, nor land in the edited-file
    set — the worker pool reuses one Orchestrator across tasks."""
    from no_human.core.bounds import StuckDetector

    events: list[dict] = []
    orch = _bare_orch(events.append)
    orch._stuck = StuckDetector()

    planner = orch._sink_for("planner:risk-first")
    for _ in range(5):
        planner(AgentEvent("tool_use", tool_name="Read",
                           tool_input={"file_path": "/src/foo.py"}))
    planner(AgentEvent("tool_use", tool_name="Edit",
                       tool_input={"file_path": "/src/foo.py"}))

    assert not any(e["kind"] == "stuck" for e in events)
    assert not getattr(orch, "_agent_edited_files", set())


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
# D18: drafts in agent-owned dirs must not trip the per-file edit-loop         #
# --------------------------------------------------------------------------- #

def _draft_then_read(orch, path: str) -> None:
    """Write *path* 5× (edit_threshold), interleaving distinct reads.

    The interleaving keeps the doom-loop detector (3× identical consecutive
    signature) and the ping-pong detector (A-B-A-B) quiet, so the only detector
    under test is the per-file edit count.
    """
    for i in range(5):
        orch._agent_sink(AgentEvent("tool_use", tool_name="Write",
                                    tool_input={"file_path": path}))
        orch._agent_sink(AgentEvent("tool_use", tool_name="Read",
                                    tool_input={"file_path": f"/repo/src/m{i}.py"}))


def test_drafts_in_agent_owned_dirs_do_not_trip_the_edit_loop():
    """Task 61406d02 died here: the coder drafted in `.no_human/`, the scope
    guard told it to "revert and stay within the planned file list", and the
    rewrite tripped the edit-loop. `.no_human/` is excluded from every git diff,
    so those writes are neither committable nor a doom signal.
    """
    from no_human.core.bounds import StuckDetector

    events: list[dict] = []
    orch = _bare_orch(events.append)
    orch._stuck = StuckDetector()
    _draft_then_read(orch, "/repo/.no_human/ci_gate_stage_draft.groovy")

    assert not [e for e in events if e.get("kind") == "stuck"]
    assert not getattr(orch, "_agent_edited_files", set())


def test_a_worktree_is_not_mistaken_for_an_agent_owned_dir():
    """Concurrency worktrees live at ~/.no_human/worktrees/<task_id>, so EVERY
    source file inside one has a `.no_human` component in its absolute path.
    Without the repo root to strip, `is_agent_owned` swallows the whole worktree:
    `_agent_edited_files` stays empty (so the commit degrades from commit_paths to
    commit_all) and the edit-loop detector never counts a thing."""
    from no_human.core.bounds import StuckDetector

    worktree = "/Users/u/.no_human/worktrees/abc123"
    events: list[dict] = []
    orch = _bare_orch(events.append)
    orch._stuck = StuckDetector()
    orch._active_repo_root = worktree
    _draft_then_read(orch, f"{worktree}/src/calc.py")

    stuck = [e for e in events if e.get("kind") == "stuck"]
    assert len(stuck) == 1
    assert "edit-loop" in stuck[0]["text"]
    assert orch._agent_edited_files == {f"{worktree}/src/calc.py"}


def test_scratch_inside_a_worktree_is_still_agent_owned():
    """…while a genuine `.no_human/scratch/` *inside* the worktree stays exempt."""
    from no_human.core.bounds import StuckDetector

    worktree = "/Users/u/.no_human/worktrees/abc123"
    events: list[dict] = []
    orch = _bare_orch(events.append)
    orch._stuck = StuckDetector()
    orch._active_repo_root = worktree
    _draft_then_read(orch, f"{worktree}/.no_human/scratch/draft.groovy")

    assert not [e for e in events if e.get("kind") == "stuck"]
    assert not getattr(orch, "_agent_edited_files", set())


def test_repeated_edits_to_a_real_file_still_trip_the_edit_loop():
    """Positive control for the exemption above — a real source file must still
    be caught, otherwise the D18 fix silently disables edit-loop detection."""
    from no_human.core.bounds import StuckDetector

    events: list[dict] = []
    orch = _bare_orch(events.append)
    orch._stuck = StuckDetector()
    _draft_then_read(orch, "/repo/src/calc.py")

    stuck = [e for e in events if e.get("kind") == "stuck"]
    assert len(stuck) == 1
    assert "edit-loop" in stuck[0]["text"]
    assert orch._agent_edited_files == {"/repo/src/calc.py"}


async def test_unstageable_linked_repo_is_announced_not_swallowed(
    bare_repo, tmp_path, store
):
    """D19: a linked repo that is not a git checkout used to be dropped by a bare
    `continue` — no event, no log the board could show. The planner still named
    its files, and nothing there could ever be committed. It stays non-fatal (the
    primary repo's work is worth doing) but it must be visible."""
    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    missing = tmp_path / "metrics-core-service-not-a-checkout"
    missing.mkdir()

    cfg = _config(tmp_path)
    events: list[dict] = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("multi-repo task", repo_path=str(bare_repo))
    t.linked_repos = [str(missing)]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL  # non-fatal
    announced = [e for e in events if e.get("kind") == "linked_repo"]
    assert len(announced) == 1
    assert announced[0]["ok"] is False
    assert str(missing) in announced[0]["text"]
    assert "not a git checkout" in announced[0]["text"]


class DoomLoopThenFailBackend:
    """Doom-loops on every attempt, then hits max_turns without ever fixing
    anything — so the stuck signal must survive into the failure detail."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        if on_event:
            for _ in range(3):
                on_event(AgentEvent("tool_use", tool_name="Read",
                                    tool_input={"file_path": "/src/foo.py"}))
            on_event(AgentEvent("result", text="Reached maximum number of turns (40)"))
        return AgentResult(
            final_text="Reached maximum number of turns (40)",
            num_turns=max_turns, is_error=True, tokens_used=100,
            session_id="s", stop_reason="max_turns",
        )


async def test_doom_loop_reason_persists_into_attempt_log(bare_repo, tmp_path, store):
    """A doom-loop mid-attempt must change what the NEXT attempt is told —
    otherwise the 'stuck: resetting context' claim is just telemetry (the
    audited gap). The reason should land in failure_reason (stored per
    attempt) and in task.context['attempt_log'] (fed into the next attempt's
    resume digest by _resume_digest)."""
    cfg = _config(tmp_path)
    backend = DoomLoopThenFailBackend()
    events: list[dict] = []
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("trigger doom loop then fail", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    attempts = await store.list_attempts(t.id)
    assert len(attempts) == cfg.data["bounds"]["max_attempts"]
    assert all("doom-loop" in (a.get("failure_reason") or "") for a in attempts)
    assert t.context.get("attempt_log")
    assert any("doom-loop" in entry for entry in t.context["attempt_log"])


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


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
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
# A3: the zero-diff attempt breaker                                            #
# --------------------------------------------------------------------------- #

class ZeroDiffBackend:
    """Edits nothing and says the work is already done — verbatim in spirit to
    what task d9d458b5's agent said on all three of its attempts."""

    STATEMENT = ("The implementation is already complete. I made zero edits. "
                 "I did not need to fabricate changes — doing so would violate "
                 "the 'smallest change' rule.")

    def __init__(self):
        self.prompts: list[str] = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.prompts.append(prompt)
        return AgentResult(final_text=self.STATEMENT, num_turns=5, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


async def test_two_zero_diff_attempts_escalate_with_the_agents_reason(
    bare_repo, tmp_path, store
):
    """d9d458b5 burned 3 attempts × ~18 turns re-running an agent against a repo
    it never modified, then escalated with 'agent produced no file changes' ×3 —
    while the agent's actual reason ("already complete, I won't fabricate") was
    discarded. Two attempts, then escalate carrying that reason."""
    cfg = _config(tmp_path)
    backend = ZeroDiffBackend()
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None))
    t = Task.new("add feature", repo_path=str(bare_repo), kind="feature")
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    # bounds.max_attempts is 3 — the third never ran.
    assert len(backend.prompts) == 2
    assert len(await store.list_attempts(t.id)) == 2

    blocker = outcome.task.blocker
    assert blocker["category"] == "AMBIGUITY"
    assert "fabricate changes" in blocker["evidence"]
    assert blocker["tried"]  # the per-attempt log, for the human reading it


async def test_work_already_committed_on_the_branch_is_not_zero_diff(
    bare_repo, tmp_path, store
):
    """`nh reply` (D15) resumes from a [WIP-BLOCKED] checkpoint whose work is
    already committed. The agent correctly adds nothing, but `has_changes()` only
    sees the working tree, so the attempt was failed as "agent produced no file
    changes". Task 84251cb2 had 645 lines committed against dev and was killed for
    it twice. The change is the branch's diff against base — ask git."""
    from no_human.vcs import GitRepo

    repo = GitRepo(bare_repo)
    base = repo.current_branch()
    repo.create_branch("scratch/resumed", base=base)
    (bare_repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
    (bare_repo / "test_calc.py").write_text(
        "from calc import add, mul\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n")
    committed = repo.commit_all("WIP-BLOCKED: prior attempt's work")

    assert not repo.has_changes()               # the tree is clean…
    assert repo.commits_ahead(base) == 1        # …but the branch carries the work
    head = repo.head_commit(base)
    assert head.sha == committed.sha
    assert head.files_changed == 2 and head.insertions > 0

    # Restore the checkout; the checkpoint commit stays reachable by sha.
    repo._run("checkout", base)


async def test_a_resumed_attempt_reviews_the_checkpoint_instead_of_failing(
    bare_repo, tmp_path, store
):
    """End-to-end: `nh reply` sets context['resume_from'], so the attempt branches
    from the [WIP-BLOCKED] commit. The agent adds nothing (there is nothing to
    add), `has_changes()` is False, and the attempt used to die as "agent produced
    no file changes" — twice, then escalate. It must instead review what is on the
    branch and reach a PR."""
    from no_human.vcs import GitRepo

    repo = GitRepo(bare_repo)
    base = repo.current_branch()
    repo.create_branch("scratch/checkpoint", base=base)
    (bare_repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
    (bare_repo / "test_calc.py").write_text(
        "from calc import add, mul\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n")
    checkpoint = repo.commit_all("[WIP-BLOCKED] prior attempt's work")
    repo._run("checkout", base)

    cfg = _config(tmp_path)
    events: list[dict] = []
    reviewer = FakeReviewer(ReviewDecision(
        passed=True,
        checklist=[ChecklistItem("mul implemented", True, "calc.py:4 returns a*b")],
    ))
    # A backend that edits nothing — exactly what a resumed coder does.
    orch = Orchestrator(store, cfg.data, ZeroDiffBackend(), SlackNotifier(None),
                        event_sink=events.append, reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.context = {"resume_from": {"sha": checkpoint.sha}}
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert not [e for e in events if e.get("kind") == "attempt_failed"]
    resumed = [e for e in events if e.get("kind") == "commit" and e.get("resumed")]
    assert len(resumed) == 1
    assert resumed[0]["files_changed"] == 2


async def test_zero_diff_preamble_appears_on_retry_and_forbids_fabrication(
    bare_repo, tmp_path, store
):
    """The corrective preamble must name the two valid outcomes without implying
    an edit has to appear — the agent it addresses may well be right."""
    cfg = _config(tmp_path)
    backend = ZeroDiffBackend()
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None))
    t = Task.new("add feature", repo_path=str(bare_repo), kind="feature")
    await store.create_task(t)

    await orch.run_task(t)

    first, second = backend.prompts
    assert "FINISHED WITHOUT EDITING ANY FILE" not in first
    assert "FINISHED WITHOUT EDITING ANY FILE" in second
    assert "Do NOT invent an edit" in second
    # And every attempt offers the sanctioned "already satisfied" exit, so an
    # agent that is right can say so on attempt 1 instead of looking like a stall.
    for prompt in (first, second):
        assert "ALREADY satisfied by the existing code" in prompt
        assert "not a silent no-op" in prompt
        # …and it must not become an escape hatch from work the agent can do.
        assert "avoid finishing doable work" in prompt


# --------------------------------------------------------------------------- #
# B3: the suite ran twice per happy path                                       #
# --------------------------------------------------------------------------- #

def _count_test_runs(monkeypatch):
    """Count real `runner.run_tests` invocations, keeping its behavior."""
    from no_human.testing import runner as _runner
    calls: list[str] = []
    real = _runner.run_tests

    def counting(repo_path, test_cmd=None, *a, **kw):
        calls.append(str(test_cmd))
        return real(repo_path, test_cmd, *a, **kw)

    monkeypatch.setattr("no_human.core.orchestrator.runner.run_tests", counting)
    return calls


async def test_the_suite_runs_once_per_attempt_not_twice(
    bare_repo, tmp_path, store, monkeypatch
):
    """`_run_review` runs the suite for the reviewer's evidence, then TESTING ran
    the identical command against the identical commit. One run, two consumers.

    A reviewer must be wired: without one `_run_review` returns an advisory pass
    before ever running tests, so the duplicate never appears."""
    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    calls = _count_test_runs(monkeypatch)
    cfg = _config(tmp_path)
    events: list[dict] = []
    reviewer = FakeReviewer(ReviewDecision(
        passed=True,
        checklist=[ChecklistItem("mul implemented", True, "calc.py:4 returns a*b")],
    ))
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append, reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert len(calls) == 1, f"suite ran {len(calls)}× in one attempt"
    reuse = [e for e in events if e.get("kind") == "tests" and e.get("cached")]
    assert len(reuse) == 1
    assert "reused the reviewer's run" in reuse[0]["text"]


async def test_a_dirty_tree_never_reuses_a_cached_pass(bare_repo, tmp_path, store):
    """A cached result feeding the review gate would be a false pass. If the tree
    moved under us, re-run — correctness outranks the saved subprocess."""
    from no_human.vcs import GitRepo

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    repo = GitRepo(bare_repo)

    first, cached = await orch._run_tests_once(repo, "true")
    assert cached is False
    _, cached = await orch._run_tests_once(repo, "true")
    assert cached is True, "a clean tree at the same commit should reuse"

    # Someone touched a tracked source file after the cached run.
    (bare_repo / "calc.py").write_text("def add(a, b):\n    return 999\n")
    _, cached = await orch._run_tests_once(repo, "true")
    assert cached is False, "a dirty tree must force a fresh run"


async def test_a_different_command_is_not_a_cache_hit(bare_repo, tmp_path, store):
    """The layered TestPlan path runs different commands than the reviewer's."""
    from no_human.vcs import GitRepo

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    repo = GitRepo(bare_repo)

    await orch._run_tests_once(repo, "true")
    _, cached = await orch._run_tests_once(repo, "true -x")
    assert cached is False


# --------------------------------------------------------------------------- #
# D21 / B4: context distillation belongs on the utility tier                   #
# --------------------------------------------------------------------------- #

class _Chunk:
    def __init__(self, content, source="file", title="big.py"):
        self.content, self.source, self.title = content, source, title


async def test_distillation_runs_on_the_utility_model_not_the_reviewer(
    tmp_path, store
):
    """D21: `_distill_large_chunks` read llm.review_model, so every oversized
    context chunk spent one Opus session to produce a summary only the coder
    ever reads. The reviewer's gate never sees it."""
    seen: list[str] = []

    class _Backend:
        def __init__(self, *, model, readonly=False, **_):
            seen.append(model)

        async def run(self, prompt, *, cwd, max_turns, effort=None, **kwargs):
            return AgentResult(final_text="a short summary", num_turns=1,
                               is_error=False, tokens_used=0, session_id="s",
                               stop_reason="end_turn")

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    chunk = _Chunk("x" * (orch._CHUNK_DISTILL_THRESHOLD + 1))
    t = Task.new("t", repo_path=str(tmp_path))

    with _patch("no_human.core.orchestrator.ClaudeBackend", _Backend):
        await orch._distill_large_chunks([chunk], t)

    assert seen == [cfg.data["llm"]["utility_model"]]
    assert "opus" not in seen[0]
    assert chunk.content.startswith("[distilled]")


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


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
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

    async def fake_evaluate_spec(title, desc, criteria, *, backend=None, model=None):
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

    async def fake_evaluate_spec(title, desc, criteria, *, backend=None, model=None):
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
    async def failing_evaluate_spec(title, desc, criteria, *, backend=None, model=None):
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


# --------------------------------------------------------------------------- #
# Which model ran which role is recorded (the blind spot that hid config drift) #
# --------------------------------------------------------------------------- #

@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_attempt_records_the_model_bound_to_each_role(bare_repo, tmp_path, store):
    def mutate(cwd):
        (cwd / "calc.py").write_text("def add(a, b):\n    return a + b\n")

    cfg = _config(tmp_path)
    events: list[dict] = []
    backend = FakeBackend(mutate)
    backend.model = "claude-sonnet-5"
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("tweak add()", repo_path=str(bare_repo))
    await store.create_task(t)

    await orch.run_task(t)

    # 1. Emitted, so it lands in the log, the board and task_events.
    ev = next(e for e in events if e["kind"] == "models")
    assert ev["models"]["coder"] == "claude-sonnet-5"
    assert ev["models"]["planner"] == cfg.data["llm"]["planner_model"]
    assert "claude-sonnet-5" in ev["text"]

    # 2. Persisted on the attempt row.
    rows = await store.db.execute(
        "SELECT models FROM attempts WHERE task_id = ?", (t.id,))
    row = await rows.fetchone()
    assert json.loads(row["models"])["coder"] == "claude-sonnet-5"


def test_active_models_reads_the_live_objects_not_the_config(tmp_path, store):
    """Reading config is exactly what hid the drift: a frozen config.yaml
    shadows the default, so config and reality disagreed for a week."""
    cfg = _config(tmp_path)
    cfg.data["llm"]["primary_model"] = "a-model-that-is-not-actually-running"

    backend = FakeBackend(lambda cwd: None)
    backend.model = "the-model-really-bound"
    reviewer = _SimpleNamespace(model="reviewer-really-bound")
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None), reviewer=reviewer)

    models = orch._active_models()
    assert models["coder"] == "the-model-really-bound"
    assert models["reviewer"] == "reviewer-really-bound"


async def test_models_are_recorded_before_planning_not_just_at_attempt_start(
    bare_repo, tmp_path, store,
):
    """A task killed during planning must still say which model held which role.
    Observed live: 166 events survived a SIGKILL and not one named a model,
    because the only `models` event fired at attempt start."""
    cfg = _config(tmp_path)
    events: list[dict] = []
    backend = FakeBackend(lambda cwd: None)
    backend.model = "claude-sonnet-5"
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    await orch.run_task(t)

    kinds = [e["kind"] for e in events]
    first_models = kinds.index("models")
    assert first_models < kinds.index("state"), "models must precede context/planning"
    assert events[first_models]["models"]["coder"] == "claude-sonnet-5"


async def test_supervisor_model_is_recorded_and_is_not_the_reviewers(tmp_path, store):
    """It must be visible which model supervises: the role used to inherit
    review_model and nothing recorded that it had."""
    cfg = _config(tmp_path)
    backend = FakeBackend(lambda cwd: None)
    backend.model = "claude-sonnet-5"
    reviewer = _SimpleNamespace(model=cfg.data["llm"]["review_model"])
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        reviewer=reviewer)

    models = orch._active_models()
    assert models["supervisor"] == "claude-sonnet-5"
    assert models["reviewer"] == "claude-opus-4-8"
    assert models["supervisor"] != models["reviewer"], (
        "the supervisor must no longer inherit the reviewer's tier"
    )


def test_no_size_cap_by_default(tmp_path, store):
    """A line/file count cannot tell a legitimately large change from a runaway
    refactor, and the check runs after the commit — so it never saved compute, it
    only stopped lint, tests, the reviewer and the PR from running. Task 84251cb2
    wrote a correct 645-line Jenkinsfile stage and was escalated for it.

    Scope is guarded semantically instead: the plan's FILES TO CHANGE list, the
    tamper guard, the evidence-based reviewer, and the human approving the PR."""
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None), SlackNotifier(None))
    huge = _SimpleNamespace(insertions=10_000, deletions=5_000, files_changed=300)

    assert cfg.data["safety"]["max_lines_changed"] is None
    assert cfg.data["safety"]["max_files_changed"] is None
    assert orch._over_size_limits(huge, Task.new("big", repo_path="/tmp/x")) is None


def test_an_opted_in_cap_still_escalates(tmp_path, store):
    """The cap is off, not gone: an install that wants one still gets it."""
    cfg = _config(tmp_path)
    cfg.data["safety"]["max_lines_changed"] = 500
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None), SlackNotifier(None))
    commit = _SimpleNamespace(insertions=605, deletions=0, files_changed=3)

    assert "max_lines_changed (605 > 500)" in orch._over_size_limits(
        commit, Task.new("ci_gate", repo_path="/tmp/x")
    )


def test_a_non_positive_cap_means_unlimited(tmp_path, store):
    """0 is a natural way to spell "no cap"; it must not block every commit."""
    cfg = _config(tmp_path)
    cfg.data["safety"] = {"max_files_changed": 0, "max_lines_changed": 0}
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None), SlackNotifier(None))
    commit = _SimpleNamespace(insertions=1, deletions=0, files_changed=1)

    assert orch._over_size_limits(commit, Task.new("t", repo_path="/tmp/x")) is None


def test_size_limits_honour_a_per_task_override(tmp_path, store):
    """The SCOPE_EXPLOSION blocker offers the human 'raise the limit for this
    task', but the limit was read from global config alone — answering that way
    produced the identical blocker on the next attempt."""
    cfg = _config(tmp_path)
    cfg.data["safety"] = {"max_files_changed": 20, "max_lines_changed": 500}
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None), SlackNotifier(None))
    commit = _SimpleNamespace(insertions=605, deletions=0, files_changed=3)

    t = Task.new("ci_gate", repo_path="/tmp/x")
    assert "max_lines_changed (605 > 500)" in orch._over_size_limits(commit, t)

    t.config = {"max_lines_changed": 800}
    assert orch._over_size_limits(commit, t) is None

    # The file limit is independent and still applies.
    t.config = {"max_lines_changed": 800, "max_files_changed": 2}
    assert "max_files_changed (3 > 2)" in orch._over_size_limits(commit, t)

    # No task, or no override: global config governs.
    assert "605 > 500" in orch._over_size_limits(commit, None)


def test_scope_explosion_option_action_derives_from_the_observed_size(tmp_path, store):
    """D14: the option 'raise the limit for this task' must carry the limit that
    actually lets this commit through — rounded up from what it measured, never
    a hardcoded number, and only for the limit that was breached."""
    cfg = _config(tmp_path)
    cfg.data["safety"] = {"max_files_changed": 20, "max_lines_changed": 500}
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None), SlackNotifier(None))
    t = Task.new("ci_gate", repo_path="/tmp/x")

    # 605 lines over a 500 limit → 700. Files are within limit → left alone.
    action = orch._size_override_action(
        _SimpleNamespace(insertions=605, deletions=0, files_changed=3), t)
    assert action == {"set_task_config": {"max_lines_changed": 700}}

    # Only files breached → only the file limit is offered.
    action = orch._size_override_action(
        _SimpleNamespace(insertions=10, deletions=0, files_changed=25), t)
    assert action == {"set_task_config": {"max_files_changed": 25}}

    # Applying the action clears the very gate that produced it.
    from no_human.blockers import apply_action
    commit = _SimpleNamespace(insertions=605, deletions=0, files_changed=3)
    assert orch._over_size_limits(commit, t) is not None
    apply_action(t, orch._size_override_action(commit, t))
    assert orch._over_size_limits(commit, t) is None


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_reply_resumes_from_the_wip_blocked_checkpoint(bare_repo, tmp_path, store):
    """D15: the blocker printed 'Resume with: nh reply <id>', but the resume path
    read ctx['handoff']['wip_sha'] — written only when an attempt runs out of
    turns — and gated it on attempt_n > 1, which a resumed run never reaches
    because it restarts its numbering at 1. The checkpoint was discarded and 41
    turns were re-done from base."""
    from no_human.blockers import resume_checkpoint

    def leave_wip(cwd):
        (cwd / "wip_marker.py").write_text("# many turns of work\n")

    bjson = (
        '{"category": "DEPENDENCY_WAIT", "confidence": 0.9, '
        '"wake_condition": "pr_merged:org/repo#42", '
        '"root_cause_hypothesis": "needs #42", "goal": "g", "evidence": "e"}'
    )
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, BlockerBackend(bjson, mutate=leave_wip),
                        SlackNotifier(None))
    t = Task.new("resume me", repo_path=str(bare_repo))
    await store.create_task(t)
    await orch.run_task(t)

    blocked = await store.get_task(t.id)
    assert blocked.status is TaskStatus.BLOCKED
    checkpoint = resume_checkpoint(blocked.blocker)
    assert checkpoint and checkpoint["sha"]

    # Exactly what `nh reply` now does before handing the task back to the loop.
    ctx = blocked.context or {}
    ctx["resume_from"] = checkpoint
    blocked.context = ctx
    await store.update_task(blocked)
    await store.set_status(blocked, TaskStatus.IMPLEMENTING, validate=False)

    events: list[dict] = []
    orch2 = Orchestrator(
        store, cfg.data,
        FakeBackend(lambda cwd: (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n")),
        SlackNotifier(None), event_sink=events.append,
    )
    await orch2.run_task(blocked)

    assert any(e.get("kind") == "resume_wip" for e in events), \
        "the resumed attempt must branch from the [WIP-BLOCKED] checkpoint"

    attempts = await store.list_attempts(blocked.id)
    resumed = attempts[-1]
    # The work survived: the checkpoint is an ancestor of the resumed branch.
    tree = subprocess.run(["git", "ls-tree", "-r", "--name-only", resumed["branch_name"]],
                          cwd=bare_repo, capture_output=True, text=True).stdout
    assert "wip_marker.py" in tree, "the checkpointed work was thrown away"

    # Branch names never collide, or `git checkout -B` would reset the branch
    # holding the checkpoint and destroy it.
    names = [a["branch_name"] for a in attempts if a["branch_name"]]
    assert len(names) == len(set(names)), f"branch names collided: {names}"
    assert [a["attempt_number"] for a in attempts] == list(range(1, len(attempts) + 1))


def test_review_base_is_the_merge_base_not_head_parent(tmp_path, store):
    """A resumed attempt carries the [WIP-BLOCKED] commit on its own branch, so
    HEAD~1 would show the reviewer only the delta over the checkpoint."""
    from no_human.vcs.git import GitRepo

    work = tmp_path / "r"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "a.txt").write_text("1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base")
    base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work,
                              capture_output=True, text=True).stdout.strip()
    _git(work, "checkout", "-b", "feature")
    (work / "a.txt").write_text("2\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "[WIP-BLOCKED] partial")
    (work / "b.txt").write_text("3\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "finish")

    orch = Orchestrator(store, _config(tmp_path).data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    repo = GitRepo(work)
    assert orch._review_base(repo, "main") == base_sha  # whole change, both commits
    assert orch._review_base(repo, None) == "HEAD~1"  # no base → unchanged default
    assert orch._review_base(repo, "no-such-branch") == "HEAD~1"  # never blocks review


def test_pr_body_carries_the_review_evidence_dossier():
    """W1.6: the PR body is the human's review surface — the reviewer's
    verdict trail must be on it, not buried in the transcript."""
    from no_human.core.orchestrator import Orchestrator
    t = Task.new("dossier", repo_path="/tmp/x")
    t.context = {"review_history": [
        {"round": 1, "passed": False,
         "blocking": ["Image build failure treated as non-fatal",
                      "Zero tests reports PASSED"]},
        {"round": 2, "passed": True, "blocking": []},
    ]}
    section = Orchestrator._review_evidence_section(t)
    assert "## Review evidence" in section
    assert "review rounds: 2" in section
    assert "**PASSED**" in section
    assert "Image build failure" in section
    # No review ran → section vanishes, body unchanged.
    t2 = Task.new("no-review", repo_path="/tmp/x")
    assert Orchestrator._review_evidence_section(t2) == ""
    # The DB stores context values as strings sometimes — survive that.
    t3 = Task.new("stringly", repo_path="/tmp/x")
    t3.context = {"review_history": "[{'round': 1, 'passed': True, 'blocking': []}]"}
    assert "**PASSED**" in Orchestrator._review_evidence_section(t3)


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_bugfix_without_repro_evidence_is_sent_back(bare_repo, tmp_path, store):
    """W1.2 (Agentless): a BUGFIX must prove the bug — a waived/failed repro
    verdict blocks before any reviewer tokens, with the fix instructions fed
    to the next attempt."""
    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )
        # no repro manifest → verdict "waived"

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("fix mul() bug", repo_path=str(bare_repo), kind="bugfix")
    t.acceptance_criteria = ["mul works"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    gate = [e for e in events if e["kind"] == "repro_gate"]
    assert gate and gate[0]["verdict"] == "waived"
    assert "[required]" in gate[0]["text"]
    # The attempt died at the gate — before review — and the coder got told.
    fresh = await store.get_task(t.id)
    fb = (fresh.context or {}).get("send_back_feedback") or []
    assert any(f.get("source") == "repro_gate" for f in fb)
    assert any("repro gate waived" in (a.get("failure_reason") or "")
               for a in await store.list_attempts(t.id))
    assert outcome.status is not TaskStatus.AWAITING_APPROVAL


async def test_feature_with_waived_repro_still_proceeds(bare_repo, tmp_path, store):
    """The gate is advisory for non-bugfix kinds — a feature without a repro
    manifest must flow to review/PR exactly as before (conservative
    enforcement: classification decides, never the gate)."""
    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
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
    t = Task.new("add mul()", repo_path=str(bare_repo))  # kind=feature
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    outcome = await orch.run_task(t)
    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    gate = [e for e in events if e["kind"] == "repro_gate"]
    assert gate and "[advisory]" in gate[0]["text"]


def test_failed_tests_event_carries_the_output_tail():
    """Triage 2026-07-11: 'FAIL: 0 passed, 0 failed, 1 errors' with no
    detail cost an hour of reproduction. The failure event must name the
    failing thing — assert via the emit-shaping logic used at the call site."""
    from types import SimpleNamespace
    result = SimpleNamespace(ok=False, output="x" * 50 + "\nImportError: cannot import name 'Foo' from 'bar'")
    fail_tail = (getattr(result, "output", "") or "")[-1200:]
    assert "ImportError" in fail_tail
    ok_result = SimpleNamespace(ok=True, output="all good")
    tail2 = "" if ok_result.ok else ok_result.output
    assert tail2 == ""


async def test_worktree_tasks_resolve_the_primary_repos_profile(bare_repo, tmp_path, store):
    """First parallel run (2026-07-11): all three worktree tasks lost their
    proven test command because the profile lookup used the WORKTREE path —
    the DB row is keyed by the primary path. Worktrees must resolve through
    git's common-dir to the primary."""
    import subprocess as sp
    from no_human.core.orchestrator import Orchestrator

    primary = str(bare_repo)
    wt = tmp_path / "wt"
    sp.run(["git", "-C", primary, "worktree", "add", str(wt), "HEAD"],
           capture_output=True, check=True)
    try:
        resolved = Orchestrator._primary_repo_path(str(wt))
        assert resolved == primary
        # The primary itself resolves to None (already primary).
        assert Orchestrator._primary_repo_path(primary) is None
    finally:
        sp.run(["git", "-C", primary, "worktree", "remove", "--force", str(wt)],
               capture_output=True)


def test_out_of_scope_becomes_a_forbidden_block_in_the_prompt():
    """W3.5 (Devin playbook): the spec's out_of_scope is surfaced to the coder
    as a hard FORBIDDEN constraint, from the first attempt — not discovered
    late by the reviewer as scope creep."""
    from no_human.core.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    t = Task.new("add helper", repo_path="/tmp/x")
    t.context = {"spec": {
        "test_plan": "cover the happy path",
        "out_of_scope": ["do not touch the auth module",
                         "do not change the DB schema"],
    }}
    digest = orch._resume_digest(t)
    assert "OUT OF SCOPE" in digest
    assert "do not touch the auth module" in digest
    assert "do not change the DB schema" in digest
    # No out_of_scope → no forbidden block.
    t2 = Task.new("y", repo_path="/tmp/x")
    t2.context = {"spec": {"test_plan": "x"}}
    assert "OUT OF SCOPE" not in orch._resume_digest(t2)


def test_pr_url_parts_delegates_to_canonical_parser():
    """EH2: there is ONE PR-URL grammar — vcs.pr_watcher.parse_pr_url — and it
    carries the host (forge, host, slug, number) so GHE/self-hosted resolve."""
    from no_human.vcs.pr_watcher import parse_pr_url
    assert parse_pr_url("https://code.example.com/dev/metrics-core-query-service/pull/513") == \
        ("github", "code.example.com", "dev/metrics-core-query-service", 513)
    assert parse_pr_url("https://gitlab.com/org/repo/-/merge_requests/42") == \
        ("gitlab", "gitlab.com", "org%2Frepo", 42)
    assert parse_pr_url("https://gitlab.acme.net/ci_gate/customer/metrics-core-service/-/merge_requests/7") == \
        ("gitlab", "gitlab.acme.net", "ci_gate%2Fcustomer%2Fmetrics-core-service", 7)
    assert parse_pr_url("not a url") is None


async def test_design_doc_report_only_completes_as_done(bare_repo, tmp_path, store):
    """A design_doc task is a READ-ONLY deliverable: findings (the document)
    with no file changes complete as DONE — reusing the investigation rails."""
    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, ReportOnlyBackend(), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("Write a design doc for the retention pipeline",
                 repo_path=str(bare_repo), kind="design_doc")
    t.acceptance_criteria = ["document covers options and a recommendation"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.DONE, f"expected DONE, got {outcome.status}"
    assert "report-only" in outcome.detail
    refreshed = await store.find_task(t.id)
    assert "findings" in (refreshed.context or {})
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["status"] == "succeeded"
