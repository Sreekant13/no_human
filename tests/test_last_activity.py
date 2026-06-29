"""Tests for _last_activity helper and TaskSummaryOut.last_activity field."""

from no_human.api.models import _last_activity, TaskSummaryOut
from no_human.core.task import Task, TaskStatus


def _task(**kw):
    defaults = dict(
        id="aaa", source="test", title="t", status=TaskStatus.PENDING,
        created_at="2026-01-01T00:00:00", updated_at="2026-01-01T01:00:00",
    )
    defaults.update(kw)
    return Task(**defaults)


def test_last_activity_no_attempts():
    t = _task()
    assert _last_activity(t, None) == "2026-01-01T01:00:00"


def test_last_activity_with_attempts():
    t = _task(updated_at="2026-01-01T01:00:00")
    attempts = [
        {"started_at": "2026-01-01T02:00:00", "completed_at": "2026-01-01T03:00:00"},
    ]
    assert _last_activity(t, attempts) == "2026-01-01T03:00:00"


def test_last_activity_attempt_still_running():
    t = _task(updated_at="2026-01-01T05:00:00")
    attempts = [
        {"started_at": "2026-01-01T02:00:00", "completed_at": None},
    ]
    # updated_at is later than started_at, no completed_at
    assert _last_activity(t, attempts) == "2026-01-01T05:00:00"


def test_last_activity_from_task_output():
    t = _task(updated_at="2026-06-01T10:00:00")
    summary = TaskSummaryOut.from_task(t, attempts=[])
    assert summary.last_activity == "2026-06-01T10:00:00"
