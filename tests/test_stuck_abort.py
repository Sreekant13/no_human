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


@pytest.mark.real_backend  # exercises the REAL ClaudeBackend.stream
# over a mocked SDK client — the hermetic stub must not replace it.
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


@pytest.mark.real_backend  # exercises the REAL ClaudeBackend.stream
# over a mocked SDK client — the hermetic stub must not replace it.
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


# ------------------- per-attempt token cap (v6 taxonomy) -------------------- #
# Four live specs burned the ENTIRE 8M lifetime budget in attempt #1 — the
# ceiling was armed with the remaining LIFETIME budget, so the bounded loop
# never got a second attempt. The attempt cap ends the ATTEMPT (work
# checkpointed, loop retries with fresh context); only the lifetime cap parks.


def test_sink_attempt_cap_beats_a_larger_remaining_budget(store, tmp_path):
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-1"
    orch._begin_attempt_accounting(
        "task-1", remaining_tokens=1_000_000, attempt_cap=1_000)
    ev = AgentEvent("usage", meta={"tokens_used": 600, "cache_read_tokens": 0,
                                   "cache_creation_tokens": 0})
    orch._agent_sink(ev, role=CODER_ROLE)  # 600 — under the cap
    with pytest.raises(BudgetAbort, match="attempt cap"):
        orch._agent_sink(ev, role=CODER_ROLE)  # 1,200 — over the cap


def test_sink_lifetime_ceiling_still_wins_when_smaller(store, tmp_path):
    orch = _orch(store, tmp_path)
    orch._active_task_id = "task-1"
    orch._begin_attempt_accounting(
        "task-1", remaining_tokens=1_000, attempt_cap=1_000_000)
    ev = AgentEvent("usage", meta={"tokens_used": 600, "cache_read_tokens": 0,
                                   "cache_creation_tokens": 0})
    orch._agent_sink(ev, role=CODER_ROLE)
    with pytest.raises(BudgetAbort, match="lifetime"):
        orch._agent_sink(ev, role=CODER_ROLE)


class _TokenGusherThenFixBackend:
    """Attempt 1 gushes past the ATTEMPT cap (WIP on disk); attempt 2 works."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            (cwd / "calc.py").write_text("def add(a, b):\n    return a + b\n# WIP\n")
            for _ in range(500):
                if on_event:
                    on_event(AgentEvent("usage", meta={
                        "tokens_used": 30_000, "cache_read_tokens": 0,
                        "cache_creation_tokens": 0}))
            raise AssertionError("attempt cap never aborted the attempt")
        if on_event:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "calc.py"}))
        _mutate(cwd)
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


async def test_attempt_cap_fails_the_attempt_and_the_loop_retries(
    store, bare_repo, tmp_path
):
    task = Task.new("add mul()", repo_path=str(bare_repo))
    task.acceptance_criteria = ["mul(a,b) returns a*b"]
    task.config = {"attempt_tokens": 50_000}  # lifetime cap (8M) stays far away
    await store.create_task(task)

    backend = _TokenGusherThenFixBackend()
    orch = _orch(store, tmp_path, backend=backend)
    outcome = await orch.run_task(task)

    # ended the ATTEMPT, not the task: the bounded loop got its retry
    assert backend.calls == 2
    reloaded = await store.find_task(task.id)
    assert (reloaded.blocker or {}).get("category") != "BUDGET_EXHAUSTED"

    attempts = await store.list_attempts(task.id)
    first = attempts[0]
    assert first["status"] == "failed"
    assert "attempt cap" in (first["failure_reason"] or "")
    # the attempt's true spend was recorded, not zero
    assert first["tokens_used"] >= 50_000

    # attempt 1's work survived as a checkpoint
    log = subprocess.run(
        ["git", "log", "--all", "--pretty=%s"], cwd=bare_repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert "[WIP-PARTIAL]" in log


class _TokenGusherWithWipBackend:
    """Gushes past the LIFETIME cap with WIP on disk — the park must keep the
    dirty tree for _raise_blocker's [WIP-BLOCKED] checkpoint (resume_commit)."""

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        (cwd / "calc.py").write_text("def add(a, b):\n    return a + b\n# WIP\n")
        for _ in range(500):
            if on_event:
                on_event(AgentEvent("usage", meta={
                    "tokens_used": 30_000, "cache_read_tokens": 0,
                    "cache_creation_tokens": 0}))
        raise AssertionError("budget cross never aborted the attempt")


async def test_lifetime_park_still_records_a_resume_checkpoint(
    store, bare_repo, tmp_path
):
    """Regression guard for the attempt-cap change: the WIP-PARTIAL checkpoint
    must fire ONLY on the attempt-cap path — a pre-emptive commit on the
    lifetime path would clean the tree before _raise_blocker's [WIP-BLOCKED]
    checkpoint and lose the blocker's resume_commit."""
    task = Task.new("add mul()", repo_path=str(bare_repo))
    task.config = {"lifetime_tokens": 50_000}
    await store.create_task(task)

    orch = _orch(store, tmp_path, backend=_TokenGusherWithWipBackend())
    await orch.run_task(task)

    reloaded = await store.find_task(task.id)
    blocker = reloaded.blocker or {}
    assert blocker.get("category") == "BUDGET_EXHAUSTED"
    assert blocker.get("resume_commit"), (
        "lifetime park lost its [WIP-BLOCKED] resume checkpoint")
    # Mutation-proof (review D8): resume_commit alone is a tautology —
    # _checkpoint_wip returns head_sha even on a clean tree. The park's
    # checkpoint must be the [WIP-BLOCKED] one; a pre-emptive [WIP-PARTIAL]
    # on this path would clean the tree first and mislabel the park point.
    log = subprocess.run(
        ["git", "log", "--all", "--pretty=%s"], cwd=bare_repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert "[WIP-BLOCKED]" in log
    assert "[WIP-PARTIAL]" not in log


# ── v8: the budget nudge closure `_build_supervisor` wires into the hook ──── #


def test_supervisor_budget_status_reads_the_armed_accounting(store, tmp_path):
    """The nudge must count EXACTLY what the hard abort counts (in/out +
    cache reads) and be scoped to the running task (worker-pool reuse)."""
    orch = _orch(store, tmp_path)
    t = Task.new("investigate", repo_path=str(tmp_path))
    hook = orch._build_supervisor(t, str(tmp_path))
    assert hook is not None and hook.budget_status is not None

    # Unarmed → None (never crashes).
    assert hook.budget_status() is None

    orch._begin_attempt_accounting(t.id, remaining_tokens=9_999_999,
                                   attempt_cap=4_000_000)
    orch._attempt_usage["tokens_used"] = 100
    orch._attempt_usage["cache_read_tokens"] = 200
    orch._attempt_usage["cache_creation_tokens"] = 7_777  # must NOT count
    assert hook.budget_status() == (300, 4_000_000)

    # Armed for a DIFFERENT task → None.
    orch._begin_attempt_accounting("other-task", remaining_tokens=1_000)
    assert hook.budget_status() is None


def test_supervisor_only_names_skills_the_coder_actually_has(store, tmp_path):
    """v10 drill (ns-7ef821b2): the supervisor told the coder to use skills
    that DON'T exist — skill-type memory TITLES leaked into its 'available
    skills' list while the coder's manifest carries only discovered on-disk
    skills. A falsifiable recommendation earned the coder's distrust of the
    whole [SUPERVISOR] channel. The supervisor's list must be exactly the
    delivered manifest names."""
    from types import SimpleNamespace

    orch = _orch(store, tmp_path)
    t = Task.new("investigate", repo_path=str(tmp_path))
    # Coder manifest: one real on-disk skill.
    orch._discovered_skills = ["real-deploy-skill"]
    orch._discovered_skills_info = [
        SimpleNamespace(name="real-deploy-skill", description="d")]
    # Skill-type memory: a title that is NOT an invocable skill.
    orch._active_memories = [
        {"type": "skill", "title": "how we once fixed the kafka retry"}]
    hook = orch._build_supervisor(t, str(tmp_path))
    assert hook is not None
    assert "real-deploy-skill" in hook.skills
    assert "how we once fixed the kafka retry" not in hook.skills


def test_supervisor_keeps_db_matched_on_disk_skills(store, tmp_path):
    """r1 finding: the delivered manifest is _discovered_skills_info, which
    deliberately RESURRECTS on-disk skills whose names match DB skill titles
    (the _kept union) — relevant_skill_names skips those from
    _discovered_skills. The supervisor's list must follow the manifest, so a
    db-matched on-disk skill (invocable under its sanitized on-disk name)
    stays recommendable when the two sets diverge."""
    from types import SimpleNamespace

    orch = _orch(store, tmp_path)
    t = Task.new("investigate", repo_path=str(tmp_path))
    # Diverge the sets: the db-matched skill is in the manifest info but NOT
    # in _discovered_skills (exactly what relevant_skill_names produces).
    orch._discovered_skills = ["plain-skill"]
    orch._discovered_skills_info = [
        SimpleNamespace(name="plain-skill", description="d"),
        SimpleNamespace(name="kafka-retry-helper", description="db-matched")]
    orch._active_memories = [
        {"type": "skill", "title": "kafka-retry-helper"}]
    hook = orch._build_supervisor(t, str(tmp_path))
    assert hook is not None
    assert "kafka-retry-helper" in hook.skills
    assert "plain-skill" in hook.skills


@pytest.mark.real_backend  # exercises the REAL ClaudeBackend.stream
async def test_stream_reports_tool_result_SIZE_from_the_user_message(tmp_path, monkeypatch):
    """PR-024 lever 1's prerequisite.

    Tool RESULTS arrive in a UserMessage — an AssistantMessage carries the
    ToolUseBlock (the call). A ToolResultBlock branch used to sit inside the
    AssistantMessage loop, so it was UNREACHABLE: 0 tool_result events across 35
    attempts against 1,497 tool_use, and the `_TOOL_RESULT_CAP` truncation a
    previous author wrote never executed once.

    The SIZE is emitted, never the text: 72% of an attempt's cost is the
    conversation re-read every turn and tool results are the payload, so the size
    distribution is what a truncation threshold must be chosen from — while
    persisting the text would bloat the DB and could capture whatever a command
    printed, including credentials.
    """
    from claude_agent_sdk import UserMessage
    from claude_agent_sdk.types import ToolResultBlock

    from no_human.agent import claude_backend
    from no_human.agent.claude_backend import ClaudeBackend, _TOOL_RESULT_CAP

    big = "x" * (_TOOL_RESULT_CAP + 500)
    small = "ok"

    async def _q(*args, **kwargs):
        yield UserMessage(content=[ToolResultBlock(tool_use_id="t1", content=big)])
        yield UserMessage(content=[ToolResultBlock(tool_use_id="t2", content=small)])

    monkeypatch.setattr(claude_backend, "query", lambda *a, **kw: _q())
    backend = ClaudeBackend(model="claude-sonnet-5")
    events = [e async for e in backend.stream("go", cwd=tmp_path, max_turns=5)]

    results = [e for e in events if e.kind == "tool_result"]
    assert len(results) == 2, f"tool results were not observed at all; got {events!r}"
    assert results[0].meta["result_chars"] == len(big)
    assert results[0].meta["over_cap"] is True
    assert results[1].meta["result_chars"] == len(small)
    assert results[1].meta["over_cap"] is False
    # JOIN KEY — without it the distribution cannot be sliced by tool, which is the
    # whole point (Bash is 62% of calls and is the unbounded one).
    assert results[0].meta["tool_use_id"] == "t1"
    assert results[1].meta["tool_use_id"] == "t2"
    # The TEXT must never be carried — DB bloat and secret capture.
    assert not results[0].text, f"tool_result must not carry the text; got {results[0].text!r}"



@pytest.mark.real_backend  # exercises the REAL ClaudeBackend.stream
async def test_stream_handles_PARALLEL_tool_results_and_measures_model_visible_text(
    tmp_path, monkeypatch
):
    """Three gaps a review found in the first version, each with its own mutation.

    * PARALLEL CALLS: the CLI batches several results into ONE UserMessage. The first
      test fed two messages of one block each, so a `break`-after-first-block mutation
      SURVIVED. This feeds one message with three blocks.
    * REPR vs TEXT: `str(content)` measured `[{'type': 'text', 'text': 'hello world'}]`
      as 41 chars for 11 of payload, and `None` as 4 rather than 0.
    * SUBAGENT SPLIT: a subagent's results are re-read in the SUBAGENT's context, not
      the main conversation, so they must be excludable from the distribution.
    """
    from claude_agent_sdk import UserMessage
    from claude_agent_sdk.types import ToolResultBlock

    from no_human.agent import claude_backend
    from no_human.agent.claude_backend import ClaudeBackend

    async def _q(*args, **kwargs):
        yield UserMessage(content=[
            ToolResultBlock(tool_use_id="a", content="12345"),
            ToolResultBlock(tool_use_id="b", content=[{"type": "text", "text": "hello world"}]),
            ToolResultBlock(tool_use_id="c", content=None),
        ])
        yield UserMessage(
            content=[ToolResultBlock(tool_use_id="d", content="sub")],
            parent_tool_use_id="toolu_parent",
        )

    monkeypatch.setattr(claude_backend, "query", lambda *a, **kw: _q())
    backend = ClaudeBackend(model="claude-sonnet-5")
    events = [e async for e in backend.stream("go", cwd=tmp_path, max_turns=5)]
    r = [e for e in events if e.kind == "tool_result"]

    assert len(r) == 4, f"parallel blocks in ONE message must each be counted; got {len(r)}"
    assert [e.meta["tool_use_id"] for e in r] == ["a", "b", "c", "d"]
    assert r[0].meta["result_chars"] == 5
    # 11 chars of payload, NOT the 41-char repr of the wrapper list.
    assert r[1].meta["result_chars"] == 11, f"repr length leaked in: {r[1].meta!r}"
    # None is 0 chars, not 4 ("None").
    assert r[2].meta["result_chars"] == 0, f"None recorded as text: {r[2].meta!r}"
    # Main-thread results are distinguishable from subagent results.
    assert r[0].meta["parent_tool_use_id"] is None
    assert r[3].meta["parent_tool_use_id"] == "toolu_parent"


@pytest.mark.real_backend  # exercises the REAL ClaudeBackend.stream
async def test_tool_use_emits_the_join_key_so_the_per_tool_slice_is_computable(
    tmp_path, monkeypatch
):
    # A review deleted `meta={"tool_use_id": block.id}` from the tool_use emit and the
    # ENTIRE suite stayed green. The commit message claimed both ends of the join key
    # were proved when only the RESULT end was. A join key with one end is not a join
    # key — and the CALL side is the half carrying `tool_name`, which is what makes the
    # per-tool slice ("Bash is 62% of calls and is the unbounded one") computable.
    from claude_agent_sdk import AssistantMessage
    from claude_agent_sdk.types import ToolUseBlock

    from no_human.agent import claude_backend
    from no_human.agent.claude_backend import ClaudeBackend

    async def _q(*args, **kwargs):
        yield AssistantMessage(
            content=[ToolUseBlock(id="toolu_x", name="Bash", input={"command": "ls"})],
            model="claude-sonnet-5",
        )

    monkeypatch.setattr(claude_backend, "query", lambda *a, **kw: _q())
    backend = ClaudeBackend(model="claude-sonnet-5")
    events = [e async for e in backend.stream("go", cwd=tmp_path, max_turns=5)]
    tu = [e for e in events if e.kind == "tool_use"]
    assert len(tu) == 1
    assert tu[0].tool_name == "Bash"
    assert tu[0].meta["tool_use_id"] == "toolu_x", (
        "without the CALL-side id the size distribution cannot be sliced BY TOOL, "
        "which is the entire purpose of collecting it"
    )


def test_result_size_flags_non_text_blocks_and_never_raises():
    # Two review findings, both in the size helper.
    # 1. An image tool result carries a large base64 payload and ZERO text, so joining
    #    only `text` recorded it as 0 chars — the SAME defect class as the repr
    #    inflation this helper replaced, opposite direction, an order of magnitude
    #    larger (11->41 vs ~1.2M->0). Now FLAGGED so it can be excluded, the way
    #    is_error and parent_tool_use_id are.
    # 2. `{"type":"text","text":null}` made it RAISE. That does not merely lose
    #    telemetry: the nearest handler terminates the stream, so the whole attempt
    #    fails as an SDK error. Telemetry must never break the session.
    from no_human.agent.claude_backend import _result_size

    assert _result_size([{"type": "text", "text": "hello world"}]) == {
        "result_chars": 11, "over_cap": False, "non_text_blocks": 0}
    img = _result_size([{"type": "image", "source": {"data": "x" * 100}}])
    assert img["result_chars"] == 0 and img["non_text_blocks"] == 1, img
    assert _result_size([{"type": "text", "text": None}])["result_chars"] == 0
    assert _result_size(None)["result_chars"] == 0
    assert _result_size("12345")["result_chars"] == 5
