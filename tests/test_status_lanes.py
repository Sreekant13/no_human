"""`nh status`'s six-bucket summary (`core.lanes.status_buckets`) — distinct
from the board's `lane_for`/`LANE_STATUSES`, which this file does not touch.

A resumed task waiting on a full worker pool must count as ``waiting``, not
``working``: it has no worker attached, and counting it as working made
`working N/max_workers` print impossible ratios (dc3b72f7)."""

from __future__ import annotations

from types import SimpleNamespace

from no_human.core.db import Store
from no_human.core.lanes import status_buckets
from no_human.core.task import Task, TaskStatus


def _t(status, id_="t1", blocker=None):
    return SimpleNamespace(id=id_, status=status, blocker=blocker)


def test_a_waiting_for_slot_task_counts_as_waiting_not_working():
    tasks = [_t(TaskStatus.IMPLEMENTING, id_="waiting-1")]
    buckets = status_buckets(tasks, waiting_for_slot_ids={"waiting-1"})
    assert buckets["waiting"] == 1
    assert buckets["working"] == 0


def test_an_ordinary_implementing_task_still_counts_as_working():
    tasks = [_t(TaskStatus.IMPLEMENTING, id_="ordinary-1")]
    buckets = status_buckets(tasks, waiting_for_slot_ids=set())
    assert buckets["working"] == 1
    assert buckets["waiting"] == 0

    # Also unaffected when the waiting set names OTHER ids only.
    buckets2 = status_buckets(tasks, waiting_for_slot_ids={"someone-else"})
    assert buckets2["working"] == 1
    assert buckets2["waiting"] == 0


def test_pending_blocked_and_terminal_buckets_are_unchanged():
    tasks = [
        _t(TaskStatus.PENDING, id_="p1"),
        _t(TaskStatus.BLOCKED, id_="b1", blocker={"wake_condition": "ci green"}),
        _t(TaskStatus.BLOCKED, id_="b2", blocker=None),
        _t(TaskStatus.AWAITING_APPROVAL, id_="a1"),
        _t(TaskStatus.AWAITING_INPUT, id_="a2"),
        _t(TaskStatus.ESCALATED, id_="a3"),
        _t(TaskStatus.PAUSED_QUOTA, id_="q1"),
        _t(TaskStatus.FAILED, id_="f1"),
        _t(TaskStatus.DONE, id_="d1"),
    ]
    # None of these ids are in the waiting-for-slot set — behavior must match
    # calling status_buckets with no waiting_for_slot_ids argument at all.
    buckets = status_buckets(tasks)
    assert buckets == {
        "needs you": 4,  # b2 (no wake condition), a1, a2, a3
        "queued": 1,      # p1
        "working": 0,
        "waiting": 2,     # b1 (wake condition), q1
        "failed": 1,
        "done": 1,
    }


async def test_tasks_waiting_for_slot_reads_the_newest_event(tmp_path):
    store = await Store(tmp_path / "nh.db").connect()
    try:
        t = Task.new("resumed WIP", repo_path="/tmp/x")
        await store.create_task(t)
        await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)

        # No events at all: not waiting.
        assert await store.tasks_waiting_for_slot() == set()

        await store.save_events(t.id, [{
            "source": "orchestrator", "kind": "waiting_for_slot",
            "text": "waiting for a worker slot (1/1 busy) since now",
        }])
        assert await store.tasks_waiting_for_slot() == {t.id}

        # A later attempt_start ends the wait — the newest event wins.
        await store.save_events(t.id, [{
            "source": "orchestrator", "kind": "attempt_start",
            "text": "attempt 1 started",
        }])
        assert await store.tasks_waiting_for_slot() == set()

        # A second, later wait period re-arms it.
        await store.save_events(t.id, [{
            "source": "orchestrator", "kind": "waiting_for_slot",
            "text": "waiting for a worker slot (1/1 busy) since later",
        }])
        assert await store.tasks_waiting_for_slot() == {t.id}
    finally:
        await store.close()


# --------------------------------------------------------------------------- #
# Review round 2 of PR #525: a wait ends at DISPATCH, and only a claimable     #
# task can be waiting                                                          #
# --------------------------------------------------------------------------- #

from no_human.core.slot_wait import is_waiting_for_slot  # noqa: E402


def _ev(kind, source="orchestrator", text=""):
    return {"source": source, "kind": kind, "text": text}


def test_a_dispatched_task_stops_waiting_at_its_first_run_event_not_attempt_start():
    """RED before the fix: only `attempt_start` ended a wait, so a PENDING
    task that got a slot read "waiting for a worker slot" through CONTEXT and
    PLANNING — minutes per task — while a worker was on it."""
    events = [_ev("waiting_for_slot"), _ev("repo_config"), _ev("state", text="context")]
    assert is_waiting_for_slot(events, status="pending") is False
    assert is_waiting_for_slot(events, status="implementing") is False
    # The wait alone is still a wait.
    assert is_waiting_for_slot([_ev("waiting_for_slot")], status="implementing") is True


def test_watcher_ticks_and_human_verbs_do_not_end_a_wait():
    """A watcher checking the task, or a human resuming it, is not a worker."""
    events = [_ev("waiting_for_slot"),
              _ev("wake_tick", source="watcher"),
              _ev("human_resume", source="human")]
    assert is_waiting_for_slot(events, status="implementing") is True


def test_a_task_that_waited_then_parked_or_ended_is_not_waiting():
    """RED before the fix: `nh show` printed the waiting line forever on a
    task that waited, was dispatched, and then parked at the approval gate or
    ended before any attempt — the newest wait-relevant event was the wait."""
    events = [_ev("waiting_for_slot")]
    for status in ("awaiting_approval", "failed", "done", "blocked",
                   "escalated", "context", "planning", "reviewing"):
        assert is_waiting_for_slot(events, status=status) is False, status


async def test_tasks_waiting_for_slot_ends_at_dispatch_and_only_for_claimable_tasks(tmp_path):
    store = await Store(tmp_path / "nh.db").connect()
    try:
        t = Task.new("fresh pending", repo_path="/tmp/x")
        await store.create_task(t)
        await store.save_events(t.id, [{
            "source": "orchestrator", "kind": "waiting_for_slot",
            "text": "waiting for a worker slot (1/1 busy) since now",
        }])
        assert await store.tasks_waiting_for_slot() == {t.id}

        # Dispatch: the first run-sourced event of ANY kind ends the wait.
        await store.save_events(t.id, [{
            "source": "orchestrator", "kind": "repo_config",
            "text": "applying the repo's .no_human.yml",
        }])
        assert await store.tasks_waiting_for_slot() == set()

        # A later wait re-arms; a watcher tick does not end it.
        await store.save_events(t.id, [
            {"source": "orchestrator", "kind": "waiting_for_slot",
             "text": "waiting for a worker slot (1/1 busy) since later"},
            {"source": "watcher", "kind": "wake_tick", "text": "checked"},
        ])
        assert await store.tasks_waiting_for_slot() == {t.id}

        # Parked at the gate with the wait as its newest event: not waiting.
        await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
        assert await store.tasks_waiting_for_slot() == set()
    finally:
        await store.close()


def test_a_dispatched_task_in_planning_counts_as_working_not_waiting():
    """The bucket side of the same defect: the DB query no longer returns a
    dispatched task, so `status_buckets` never sees it in the waiting set —
    and even if a stale caller passed it, a non-claimable status would fall
    through to its own bucket. Pin both halves."""
    planning = _t(TaskStatus.PLANNING, id_="p1")
    assert status_buckets([planning], waiting_for_slot_ids=set())["working"] == 1
