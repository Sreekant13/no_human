"""Deterministic runaway abort (docs/ARCH_REVIEW.md B2 #1 + #2).

The stuck/doom-loop detectors used to emit telemetry and let the attempt run
on — the LLM supervisor, which fails open, held the only abort authority — so
a recognized loop could burn the full 500-turn budget (live precedent: 3.4M
cache-read in 41 turns, ~12× headroom at the current cap). And the lifetime
token cap was checked only at attempt boundaries, so one attempt could blow
through the whole 8M unwatched.

These tests pin the deterministic teeth:

1. a HARD detector fire (far above the advisory thresholds) raises StuckAbort
   at the next event boundary → the attempt FAILS with its work checkpointed
   and the bounded loop retries with fresh context — the task is not parked;
2. per-turn usage events accumulate in the sink and raise BudgetAbort the
   moment the attempt crosses the task's remaining lifetime budget → the
   attempt records its true spend and the task parks behind the same
   BUDGET_EXHAUSTED blocker the boundary check raises.
"""

import subprocess

import pytest

from no_human.agent.claude_backend import AgentEvent, AgentResult
from no_human.core.bounds import StuckDetector
from no_human.core.orchestrator import (
    CODER_ROLE,
    BudgetAbort,
    Orchestrator,
    StuckAbort,
)
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier

from .test_e2e_orchestrator import FakeBackend, _config, bare_repo, store  # noqa: F401


def _mutate(cwd):
    (cwd / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
    )


def _orch(store, tmp_path, backend=None, events=None):
    cfg = _config(tmp_path)
    return Orchestrator(
        store, cfg.data, backend or FakeBackend(_mutate), SlackNotifier(None),
        event_sink=(events.append if events is not None else None),
    )


# ------------------------------ the sink ----------------------------------- #


def test_sink_aborts_on_hard_doom_loop(store, tmp_path):
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-1"
    orch._stuck = StuckDetector()
    ev = AgentEvent("tool_use", tool_name="Bash", tool_input={"command": "pytest -x"})
    with pytest.raises(StuckAbort, match="doom-loop"):
        for _ in range(orch._stuck.doom_loop_abort):
            orch._agent_sink(ev, role=CODER_ROLE)


def test_sink_never_aborts_below_the_hard_threshold(store, tmp_path):
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-1"
    orch._stuck = StuckDetector()
    ev = AgentEvent("tool_use", tool_name="Bash", tool_input={"command": "pytest -x"})
    for _ in range(orch._stuck.doom_loop_abort - 1):
        orch._agent_sink(ev, role=CODER_ROLE)  # advisory only — must not raise


@pytest.mark.parametrize("role", ["planner", "reviewer", "aggregator"])
def test_only_the_implementer_session_stuck_aborts(store, tmp_path, role):
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-1"
    orch._stuck = StuckDetector()
    ev = AgentEvent("tool_use", tool_name="Bash", tool_input={"command": "pytest -x"})
    for _ in range(orch._stuck.doom_loop_abort + 3):
        orch._agent_sink(ev, role=role)  # must not raise


def test_sink_aborts_when_spend_crosses_the_remaining_budget(store, tmp_path):
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-1"
    orch._begin_attempt_accounting("task-1", remaining_tokens=1_000)
    ev = AgentEvent("usage", meta={"tokens_used": 300, "cache_read_tokens": 300,
                                   "cache_creation_tokens": 0})
    orch._agent_sink(ev, role=CODER_ROLE)  # 600 — under the ceiling
    with pytest.raises(BudgetAbort):
        orch._agent_sink(ev, role=CODER_ROLE)  # 1,200 — over


def test_budget_ceiling_is_scoped_to_the_running_task(store, tmp_path):
    """The worker pool reuses one Orchestrator — task B's usage must never be
    charged against task A's ceiling (same scoping rule as _cancel_reason)."""
    orch = _orch(store, tmp_path)
    orch._begin_attempt_accounting("task-1", remaining_tokens=100)
    orch._active_task_id = "task-2"
    ev = AgentEvent("usage", meta={"tokens_used": 500, "cache_read_tokens": 0,
                                   "cache_creation_tokens": 0})
    orch._agent_sink(ev, role=CODER_ROLE)  # must not raise


def test_cache_creation_does_not_count_toward_the_cap(store, tmp_path):
    """The lifetime ledger (db.lifetime_usage) counts in/out + cache READS; the
    running total must count the same things or the two gates disagree."""
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-1"
    orch._begin_attempt_accounting("task-1", remaining_tokens=1_000)
    ev = AgentEvent("usage", meta={"tokens_used": 100, "cache_read_tokens": 0,
                                   "cache_creation_tokens": 5_000})
    orch._agent_sink(ev, role=CODER_ROLE)  # must not raise


# ------------------------------ the backend -------------------------------- #


async def test_stream_yields_usage_events_per_assistant_message(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage
    from claude_agent_sdk.types import TextBlock

    from no_human.agent import claude_backend
    from no_human.agent.claude_backend import ClaudeBackend

    msg = AssistantMessage(
        content=[TextBlock(text="working…")], model="claude-sonnet-5",
        usage={"input_tokens": 1_000, "output_tokens": 200,
               "cache_read_input_tokens": 50_000, "cache_creation_input_tokens": 7},
    )

    async def _q(*args, **kwargs):
        yield msg

    monkeypatch.setattr(claude_backend, "query", lambda *a, **kw: _q())
    backend = ClaudeBackend(model="claude-sonnet-5")
    events = [e async for e in backend.stream("go", cwd=tmp_path, max_turns=5)]

    usage = [e for e in events if e.kind == "usage"]
    assert len(usage) == 1
    assert usage[0].meta["tokens_used"] == 1_200
    assert usage[0].meta["cache_read_tokens"] == 50_000
    assert usage[0].meta["cache_creation_tokens"] == 7


async def test_per_message_sum_equals_result_cumulative(tmp_path, monkeypatch):
    """Review F2: an ABORTED attempt records the per-message SUM while a normal
    attempt records the ResultMessage cumulative. Pin our arithmetic: on the
    same stream, the accumulated usage events must equal what the result event
    reports — if the SDK's semantics ever drift, this is the tripwire."""
    from claude_agent_sdk import AssistantMessage, ResultMessage
    from claude_agent_sdk.types import TextBlock

    from no_human.agent import claude_backend
    from no_human.agent.claude_backend import ClaudeBackend

    msgs = [
        AssistantMessage(
            content=[TextBlock(text=f"turn {i}")], model="claude-sonnet-5",
            usage={"input_tokens": 100 * i, "output_tokens": 10 * i,
                   "cache_read_input_tokens": 1_000 * i,
                   "cache_creation_input_tokens": i},
        )
        for i in (1, 2, 3)
    ]
    total_in_out = sum(100 * i + 10 * i for i in (1, 2, 3))
    total_read = sum(1_000 * i for i in (1, 2, 3))
    result = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=3, session_id="s", total_cost_usd=0.0,
        usage={"input_tokens": 600, "output_tokens": 60,
               "cache_read_input_tokens": 6_000,
               "cache_creation_input_tokens": 6},
        result="done",
    )

    async def _q(*args, **kwargs):
        for m in msgs:
            yield m
        yield result

    monkeypatch.setattr(claude_backend, "query", lambda *a, **kw: _q())
    backend = ClaudeBackend(model="claude-sonnet-5")
    events = [e async for e in backend.stream("go", cwd=tmp_path, max_turns=5)]

    usage_events = [e for e in events if e.kind == "usage"]
    summed = sum(e.meta["tokens_used"] for e in usage_events)
    summed_read = sum(e.meta["cache_read_tokens"] for e in usage_events)
    (result_ev,) = [e for e in events if e.kind == "result"]

    assert summed == total_in_out == result_ev.meta["tokens_used"]
    assert summed_read == total_read == result_ev.meta["cache_read_tokens"]


# --------------------------- end to end ------------------------------------ #


class _DoomLoopThenFixBackend:
    """Attempt 1 doom-loops with WIP on disk; attempt 2 does the real work."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            (cwd / "calc.py").write_text("def add(a, b):\n    return a + b\n# WIP\n")
            for _ in range(500):
                if on_event:
                    on_event(AgentEvent("tool_use", tool_name="Bash",
                                        tool_input={"command": "pytest -x"}))
            raise AssertionError("hard doom-loop never aborted the attempt")
        if on_event:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "calc.py"}))
        _mutate(cwd)
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


async def test_hard_doom_loop_fails_the_attempt_and_the_loop_retries(
    store, bare_repo, tmp_path
):
    task = Task.new("add mul()", repo_path=str(bare_repo))
    task.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(task)

    backend = _DoomLoopThenFixBackend()
    orch = _orch(store, tmp_path, backend=backend)
    outcome = await orch.run_task(task)

    # ended the attempt, not the task: the bounded loop got its retry
    assert backend.calls == 2
    assert outcome.status is not TaskStatus.BLOCKED

    attempts = await store.list_attempts(task.id)
    first = attempts[0]
    assert first["status"] == "failed"
    assert "doom-loop" in (first["failure_reason"] or "")

    # attempt 1's work survived as a checkpoint (on attempt 1's own branch —
    # attempt 2 branches fresh from base, so it is not in HEAD's history)
    log = subprocess.run(
        ["git", "log", "--all", "--pretty=%s"], cwd=bare_repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert "[WIP-PARTIAL]" in log


class _TokenGusherBackend:
    """Burns tokens forever; only the mid-attempt cap can stop it."""

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        _mutate(cwd)
        for _ in range(500):
            if on_event:
                on_event(AgentEvent("usage", meta={
                    "tokens_used": 30_000, "cache_read_tokens": 0,
                    "cache_creation_tokens": 0}))
        raise AssertionError("budget cross never aborted the attempt")


async def test_mid_attempt_budget_cross_parks_behind_budget_exhausted(
    store, bare_repo, tmp_path
):
    task = Task.new("add mul()", repo_path=str(bare_repo))
    task.config = {"lifetime_tokens": 50_000}
    await store.create_task(task)

    orch = _orch(store, tmp_path, backend=_TokenGusherBackend())
    outcome = await orch.run_task(task)

    assert outcome.status is not TaskStatus.DONE
    reloaded = await store.find_task(task.id)
    assert (reloaded.blocker or {}).get("category") == "BUDGET_EXHAUSTED"

    # the attempt's true spend was recorded — an aborted attempt must not
    # report zero tokens (that's how 21.2M once slipped past every cap)
    attempts = await store.list_attempts(task.id)
    assert attempts[0]["tokens_used"] >= 50_000
