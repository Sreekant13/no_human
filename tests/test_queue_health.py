"""Queue health: stuck flag + drain ETA (D2 #4). Zero LLM, pure timestamps."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from no_human.core.db import Store
from no_human.core.health import queue_health
from no_human.core.task import Task, TaskStatus


@pytest.fixture
async def store():
    s = await Store(":memory:").connect()
    yield s
    await s.close()


async def _task(store, status: TaskStatus, *, updated_min_ago: int = 0):
    t = Task.new(f"t-{status.value}-{updated_min_ago}", repo_path="/r")
    await store.create_task(t)
    await store.set_status(t, status, validate=False)
    if updated_min_ago:
        ts = (datetime.now(timezone.utc)
              - timedelta(minutes=updated_min_ago)).isoformat()
        await store.db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?",
                               (ts, t.id))
        await store.db.commit()
    return t


async def test_empty_queue_is_never_stuck(store):
    h = await queue_health(store)
    assert h.open_tasks == 0 and h.stuck is False and h.eta_minutes is None


async def test_open_work_with_no_completions_is_stuck(store):
    await _task(store, TaskStatus.IMPLEMENTING)
    await _task(store, TaskStatus.DONE, updated_min_ago=120)  # stale completion
    h = await queue_health(store, stuck_after_minutes=30)
    assert h.stuck is True
    assert "nothing has completed in 30 minutes" in h.stuck_reason


async def test_recent_completion_clears_stuck_and_gives_an_eta(store):
    for _ in range(4):
        await _task(store, TaskStatus.PENDING)
    # 2 completions in the last 30 minutes → 0.0667/min → 4 open ≈ 60 min
    await _task(store, TaskStatus.DONE, updated_min_ago=5)
    await _task(store, TaskStatus.FAILED, updated_min_ago=10)

    h = await queue_health(store, stuck_after_minutes=30, window_minutes=30)
    assert h.stuck is False
    assert h.completed_in_window == 2
    assert h.eta_minutes == pytest.approx(60.0, rel=0.01)


async def test_tasks_at_a_human_gate_are_not_open_work(store):
    """A board full of PRs awaiting approval is SUCCESS, not a stall — the
    queue owes nothing until the human acts."""
    await _task(store, TaskStatus.AWAITING_APPROVAL)
    await _task(store, TaskStatus.ESCALATED)
    h = await queue_health(store, stuck_after_minutes=1)
    assert h.open_tasks == 0
    assert h.at_gate == 2
    assert h.stuck is False


async def test_eta_is_none_not_zero_when_unknowable(store):
    await _task(store, TaskStatus.PENDING)
    h = await queue_health(store, window_minutes=30)
    assert h.eta_minutes is None, "an unknown ETA must never render as a number"
