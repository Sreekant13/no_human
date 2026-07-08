"""Autonomy telemetry (megaplan P0) — metric computation over tasks/attempts."""

import pytest

from no_human.core.autonomy import compute_autonomy_metrics
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "t.db").connect()
    yield s
    await s.close()


async def _mk(store, title, status, *, blocker=None):
    t = Task.new(title, repo_path="/r")
    await store.create_task(t)
    if status is not TaskStatus.PENDING:
        await store.set_status(t, status, validate=False)
    if blocker is not None:
        t.blocker = blocker
        t.status = status
        await store.update_task(t)
    return t


async def test_empty_db_rates_are_none(store):
    rep = await compute_autonomy_metrics(store)
    assert rep.settled_tasks == 0
    assert rep.pr_reached_rate is None
    assert rep.touchpoint_rate is None


async def test_touchpoint_and_pr_reached_rates(store):
    # 2 PR-reached (awaiting_approval, done), 2 touchpoints (escalated, blocked),
    # 1 still active (implementing → excluded from settled).
    await _mk(store, "pr1", TaskStatus.AWAITING_APPROVAL)
    await _mk(store, "pr2", TaskStatus.DONE)
    await _mk(store, "esc", TaskStatus.ESCALATED,
              blocker={"category": "STAGNATION"})
    await _mk(store, "blk", TaskStatus.BLOCKED,
              blocker={"category": "MISSING_ACCESS"})
    await _mk(store, "wip", TaskStatus.IMPLEMENTING)

    rep = await compute_autonomy_metrics(store)
    assert rep.total_tasks == 5
    assert rep.settled_tasks == 4          # wip excluded
    assert rep.pr_reached == 2
    assert rep.touchpoint_tasks == 2
    assert rep.pr_reached_rate == 0.5
    assert rep.touchpoint_rate == 0.5
    assert rep.blocker_categories == {"STAGNATION": 1, "MISSING_ACCESS": 1}


async def test_turn_exhaustion_empty_diff_counted(store):
    t = await _mk(store, "big", TaskStatus.FAILED)
    aid1 = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        aid1, failure_reason="Agent run did not complete (max_turns)", diff=None)
    aid2 = await store.create_attempt(t.id, 2)
    # A real failure with a diff is NOT counted as turn-exhaustion-empty.
    await store.update_attempt(
        aid2, failure_reason="review failed", diff="diff --git a b")

    rep = await compute_autonomy_metrics(store)
    assert rep.turn_exhaustion_empty == 1


async def test_days_window_filters(store):
    # created_at is only persisted on INSERT, so backdate before create_task.
    t = Task.new("old", repo_path="/r")
    t.created_at = "2000-01-01T00:00:00+00:00"
    await store.create_task(t)
    await store.set_status(t, TaskStatus.DONE, validate=False)
    rep = await compute_autonomy_metrics(store, days=1)
    assert rep.total_tasks == 0
