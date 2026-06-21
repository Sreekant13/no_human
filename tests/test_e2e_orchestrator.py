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
