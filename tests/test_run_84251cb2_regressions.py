"""Three defects the live CI_GATE run (task 84251cb2, 2026-07-10) exposed.

Each one is a signal that lied, in the same way the plan's worst bugs lied: it
reported success or zero rather than the truth, and nothing noticed.
"""

import asyncio
import pathlib

import pytest

from no_human.blockers.taxonomy import Blocker, BlockerCategory
from no_human.core.bounds import Bounds
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "t.db").connect()
    yield s
    await s.close()


# ---- 1. the attempts that burn most reported zero burn ---------------------- #


def test_error_result_event_carries_the_cache_counters():
    """`run()` keeps the LAST result event. On max_turns the SDK emits a real
    ResultMessage and THEN raises, so `stream()` yields a corrective error event
    — which used to omit the cache counters. Attempt 11 of task 84251cb2 burned
    3,424,504 cache-read tokens and recorded 0.

    Reverting the `last_cache_read` plumbing leaves these at 0.
    """
    from no_human.agent import claude_backend

    src = pathlib.Path(claude_backend.__file__).read_text()
    error_block = src.split('except Exception as exc:  # noqa: BLE001 — SDK raises')[1]
    error_block = error_block.split("return")[0]
    assert '"cache_read_tokens": last_cache_read' in error_block
    assert '"cache_creation_tokens": last_cache_creation' in error_block


async def test_agent_result_reports_cache_tokens_from_an_errored_run():
    """End to end through the AgentEvent -> AgentResult mapping run() performs."""
    from no_human.agent.claude_backend import AgentEvent, AgentResult

    # what stream() now yields when the SDK raises after a real ResultMessage
    err = AgentEvent("result", text="maximum number of turns", meta={
        "num_turns": 41, "is_error": True, "tokens_used": 42187,
        "session_id": "s", "stop_reason": "max_turns", "denials": [],
        "cache_read_tokens": 3_424_504, "cache_creation_tokens": 1_000,
    })
    m = err.meta
    result = AgentResult(
        final_text=err.text, num_turns=int(m["num_turns"]),
        is_error=True, tokens_used=int(m["tokens_used"]), session_id="s",
        stop_reason=m["stop_reason"],
        cache_read_tokens=int(m.get("cache_read_tokens", 0)),
        cache_creation_tokens=int(m.get("cache_creation_tokens", 0)),
    )
    assert result.cache_read_tokens == 3_424_504
    assert result.stop_reason == "max_turns"


# ---- 2. a resumed task silently discarded the parked attempt's work --------- #


def _blocker_with_checkpoint(sha: str) -> dict:
    b = Blocker(
        category=BlockerCategory.NOVEL_UNKNOWN,
        transient=False, confidence=0.9, goal="g",
        root_cause_hypothesis="bounds exhausted",
    )
    b.resume_commit = sha
    b.resume_branch = "dev"
    return b.to_dict()


async def test_wake_resume_adopts_the_blockers_checkpoint(store):
    """`_run_attempt` prefers ctx['resume_from'] over the WIP sha, so a resume
    that does not refresh it branches from a stale commit and throws away every
    file the parked attempt committed."""
    from no_human.blockers.wake import WakeWatcher

    task = Task.new("t", repo_path="/r")
    task.context = {"resume_from": {"sha": "0e22fe3d" * 5, "branch": "dev"}}
    task.blocker = _blocker_with_checkpoint("06cd40fc" * 5)
    await store.create_task(task)
    await store.set_status(task, TaskStatus.BLOCKED, validate=False)

    w = WakeWatcher(store, {}, on_event=lambda *a, **k: None)
    await w._resume(task)

    reloaded = await store.find_task(task.id)
    assert reloaded.context["resume_from"]["sha"] == "06cd40fc" * 5, (
        "resume must continue from the checkpoint the blocker recorded"
    )
    assert reloaded.status is TaskStatus.IMPLEMENTING


# ---- 3. `attempts.status` was not a completion signal ----------------------- #


async def test_a_new_attempt_closes_a_dead_in_progress_row(store):
    """Attempts 1, 6 and 9 of task 84251cb2 are still 'in_progress' because the
    process was killed. Left alone, `attempts.status` cannot be trusted."""
    task = Task.new("t", repo_path="/r")
    await store.create_task(task)

    a1 = await store.create_attempt(task.id, 1)   # dies without closing
    a2 = await store.create_attempt(task.id, 2)

    rows = {r["id"]: r["status"] for r in await store.list_attempts(task.id)}
    assert rows[a1] == "interrupted"
    assert rows[a2] == "in_progress"


async def test_closing_dead_rows_never_touches_a_finished_attempt(store):
    task = Task.new("t", repo_path="/r")
    await store.create_task(task)
    a1 = await store.create_attempt(task.id, 1)
    await store.update_attempt(a1, status="failed")
    await store.create_attempt(task.id, 2)

    rows = {r["id"]: r["status"] for r in await store.list_attempts(task.id)}
    assert rows[a1] == "failed", "a closed attempt keeps its real outcome"


async def test_closing_dead_rows_is_scoped_to_the_task(store):
    """Serial attempts are per task; another task's live attempt must survive."""
    t1, t2 = Task.new("a", repo_path="/r"), Task.new("b", repo_path="/r")
    await store.create_task(t1)
    await store.create_task(t2)
    other = await store.create_attempt(t2.id, 1)
    await store.create_attempt(t1.id, 1)
    await store.create_attempt(t1.id, 2)

    rows = {r["id"]: r["status"] for r in await store.list_attempts(t2.id)}
    assert rows[other] == "in_progress"


# ---- 4. the turn budget had three sources of truth -------------------------- #


def test_bounds_defaults_have_exactly_one_source_of_truth():
    """The dataclass, `from_config` and DEFAULT_CONFIG each carried the same
    literals. `config.yaml` pinned max_turns_per_attempt=40, shadowing the 60
    default, and task 84251cb2 escalated mid-fix at turn 41."""
    from no_human.config import DEFAULT_CONFIG

    defaults = Bounds()
    for key, value in DEFAULT_CONFIG["bounds"].items():
        assert getattr(defaults, key) == value, (
            f"DEFAULT_CONFIG['bounds']['{key}'] has drifted from Bounds.{key}"
        )
    # an empty config resolves to the dataclass defaults, not to stale literals
    assert Bounds.from_config({}) == defaults
    assert Bounds.from_config(None) == defaults


def test_from_config_overrides_only_what_it_is_given():
    b = Bounds.from_config({"max_attempts": 2})
    assert b.max_attempts == 2
    assert b.max_turns_per_attempt == Bounds().max_turns_per_attempt


def test_from_config_ignores_unknown_keys_from_a_hand_edited_config():
    assert Bounds.from_config({"max_attempts": 2, "typo_key": 1}).max_attempts == 2


def test_turn_budget_covers_the_measured_need():
    """22 turns to reach the reviewer, >40 to act on its findings (84251cb2).
    The budget must comfortably clear that, or an attempt dies mid-fix."""
    assert Bounds().max_turns_per_attempt >= 100
    assert Bounds().turns_for(complex_task=True) >= 150


def test_a_high_turn_cap_is_only_safe_because_pause_is_real():
    """The cap is the last-resort stop for a thrashing agent, and the only one
    (constraint #5 forbids mid-attempt interruption). At 500 turns the operator
    MUST be able to stop a runaway, or a doom-loop burns to the cap. Deleting
    cooperative cancellation would silently make this budget unsafe."""
    from no_human.core.db import Store as _Store
    from no_human.core.orchestrator import CancelRequested, Orchestrator

    assert hasattr(Orchestrator, "_watch_for_cancel")
    assert hasattr(Orchestrator, "_honor_cancel")
    assert hasattr(_Store, "request_cancel")
    assert issubclass(CancelRequested, Exception)
