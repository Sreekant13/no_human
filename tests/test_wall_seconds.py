"""_wall_seconds — the time half of the per-task cost meter."""

from no_human.api.models import _wall_seconds


def test_basic_duration():
    assert _wall_seconds("2026-07-12T10:00:00+00:00", "2026-07-12T10:02:30+00:00") == 150.0


def test_handles_z_suffix():
    assert _wall_seconds("2026-07-12T10:00:00Z", "2026-07-12T10:00:05Z") == 5.0


def test_never_negative():
    # last_activity before created (clock skew / bad data) clamps to 0, not negative.
    assert _wall_seconds("2026-07-12T10:00:05Z", "2026-07-12T10:00:00Z") == 0.0


def test_missing_or_unparseable_is_none_not_raise():
    assert _wall_seconds(None, "2026-07-12T10:00:00Z") is None
    assert _wall_seconds("2026-07-12T10:00:00Z", None) is None
    assert _wall_seconds("not-a-date", "2026-07-12T10:00:00Z") is None


def test_taskout_surfaces_the_three_cost_axes_together():
    """The drawer's TaskOut carries tokens + wall_seconds + attempt_count so the
    cost meter shows all three."""
    from no_human.api.models import TaskOut
    from no_human.core.task import Task

    t = Task.new("t", repo_path="/r")
    t.created_at = "2026-07-12T10:00:00Z"
    t.updated_at = "2026-07-12T10:03:00Z"
    attempts = [
        {"tokens_used": 1000, "completed_at": "2026-07-12T10:01:00Z"},
        {"tokens_used": 1500, "completed_at": "2026-07-12T10:03:00Z"},
    ]
    out = TaskOut.from_task(t, attempts)
    assert out.total_tokens == 2500
    assert out.attempt_count == 2
    assert out.wall_seconds == 180.0  # 10:00:00 → 10:03:00
