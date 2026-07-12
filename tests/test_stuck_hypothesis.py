"""C2: after two failed attempts, an independent utility-model diagnosis
(root-cause hypothesis + one different approach) rides into the next
attempt's preamble — the StuckDetector reset context but never said WHY."""

from unittest.mock import patch as _patch

from no_human.agent.claude_backend import AgentResult
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus


class _HypoBackend:
    def __init__(self, text):
        self.text = text
        self.prompts = []

    async def run(self, prompt, **kw):
        self.prompts.append(prompt)
        return AgentResult(final_text=self.text, num_turns=1, is_error=False,
                           tokens_used=10, session_id="s", stop_reason="end")


def _orch_min(store=None):
    orch = object.__new__(Orchestrator)
    orch.config = {}
    orch._sink = lambda e: None
    orch.store = store
    return orch


class _FakeStore:
    def __init__(self):
        self.updates = []

    async def update_task(self, task):
        self.updates.append(task)


async def test_hypothesis_stored_and_emitted_after_two_failures(tmp_path):
    orch = _orch_min(_FakeStore())
    events = []
    orch._sink = events.append
    t = Task.new("fix the flaky export", repo_path=str(tmp_path))
    t.context = {"attempt_log": [
        "attempt 1: tests failed: TimeoutError in export worker",
        "attempt 2: tests failed: TimeoutError in export worker",
    ]}
    fake = _HypoBackend("HYPOTHESIS: the worker polls a queue that the test "
                        "never seeds.\nTRY INSTEAD: seed the queue fixture "
                        "before starting the worker.")
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        await orch._generate_stuck_hypothesis(t)

    assert "HYPOTHESIS" in t.context["stuck_hypothesis"]
    assert any(e.get("kind") == "stuck_hypothesis" for e in events)
    # the diagnosis prompt showed the actual attempt log
    assert "TimeoutError" in fake.prompts[0]


async def test_hypothesis_skipped_below_two_failures(tmp_path):
    orch = _orch_min(_FakeStore())
    t = Task.new("x", repo_path=str(tmp_path))
    t.context = {"attempt_log": ["attempt 1: failed"]}
    fake = _HypoBackend("HYPOTHESIS: x\nTRY INSTEAD: y")
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        await orch._generate_stuck_hypothesis(t)
    assert "stuck_hypothesis" not in (t.context or {})
    assert fake.prompts == []


async def test_malformed_reply_is_dropped_not_stored(tmp_path):
    orch = _orch_min(_FakeStore())
    t = Task.new("x", repo_path=str(tmp_path))
    t.context = {"attempt_log": ["attempt 1: f", "attempt 2: f"]}
    fake = _HypoBackend("I could not determine anything useful.")
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        await orch._generate_stuck_hypothesis(t)
    assert "stuck_hypothesis" not in (t.context or {})


async def test_backend_error_never_blocks(tmp_path):
    orch = _orch_min(_FakeStore())
    t = Task.new("x", repo_path=str(tmp_path))
    t.context = {"attempt_log": ["attempt 1: f", "attempt 2: f"]}

    class _Boom:
        async def run(self, *a, **k):
            raise RuntimeError("utility model unavailable")

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=_Boom()):
        await orch._generate_stuck_hypothesis(t)  # must not raise
    assert "stuck_hypothesis" not in (t.context or {})


def test_preamble_carries_the_diagnosis():
    orch = object.__new__(Orchestrator)
    orch.config = {}
    orch.ci_runner = None
    orch._active_profile = None
    orch._active_memories = None
    t = Task.new("x", repo_path="/tmp/r")
    t.status = TaskStatus.IMPLEMENTING
    t.acceptance_criteria = ["works"]
    t.context = {
        "attempt_log": ["attempt 1: failed", "attempt 2: failed"],
        "stuck_hypothesis": "HYPOTHESIS: queue never seeded.\nTRY INSTEAD: seed it.",
    }
    prompt = orch._build_implement_prompt(t, "/tmp/r")
    assert "AN INDEPENDENT DIAGNOSIS" in prompt
    assert "queue never seeded" in prompt
    # advisory framing is mandatory — the coder must verify, not trust
    assert "verify before trusting" in prompt


async def test_missing_repo_path_skips_the_call(tmp_path):
    orch = _orch_min(_FakeStore())
    t = Task.new("x", repo_path="")
    t.context = {"attempt_log": ["attempt 1: f", "attempt 2: f"]}
    fake = _HypoBackend("HYPOTHESIS: x\nTRY INSTEAD: y")
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        await orch._generate_stuck_hypothesis(t)
    assert fake.prompts == []


async def test_operator_reply_reaches_the_diagnosis(tmp_path):
    """A resumed task's diagnosis must not contradict the human's guidance."""
    orch = _orch_min(_FakeStore())
    t = Task.new("x", repo_path=str(tmp_path))
    t.context = {
        "attempt_log": ["attempt 1: f", "attempt 2: f"],
        "human_replies": ["ignore the queue — the real issue is the API contract"],
    }
    fake = _HypoBackend("HYPOTHESIS: x\nTRY INSTEAD: y")
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        await orch._generate_stuck_hypothesis(t)
    assert "API contract" in fake.prompts[0]
