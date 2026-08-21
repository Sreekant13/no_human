"""Unit coverage for the priority vocabulary itself: `normalise_priority`
(fail-loud — used on write paths, CLI/API) and `priority_rank` (fail-soft —
used on the claim path, where a legacy/hand-written row must never break
scheduling), plus `Task.from_row`'s NULL-priority default.
"""

from __future__ import annotations

from no_human.core.task import (
    DEFAULT_PRIORITY,
    PRIORITY_ORDER,
    Task,
    normalise_priority,
    priority_rank,
)


def test_priority_order_is_high_medium_low():
    assert PRIORITY_ORDER == ("high", "medium", "low")
    assert DEFAULT_PRIORITY == "medium"


def test_priority_rank_orders_high_before_medium_before_low():
    assert priority_rank("high") < priority_rank("medium") < priority_rank("low")


def test_priority_rank_none_ranks_as_medium():
    assert priority_rank(None) == priority_rank("medium")


def test_priority_rank_never_raises_on_garbage():
    """The claim path must never break scheduling over a corrupt/legacy
    value — unknown tokens fail soft to medium's rank."""
    assert priority_rank("garbage") == priority_rank("medium")
    assert priority_rank("") == priority_rank("medium")
    assert priority_rank(42) == priority_rank("medium")


def test_normalise_priority_trims_and_lowercases():
    assert normalise_priority(" HIGH ") == "high"
    assert normalise_priority("Low") == "low"


def test_normalise_priority_none_or_empty_defaults_to_medium():
    assert normalise_priority(None) == DEFAULT_PRIORITY
    assert normalise_priority("") == DEFAULT_PRIORITY
    assert normalise_priority("   ") == DEFAULT_PRIORITY


def test_normalise_priority_rejects_unknown_value():
    """The write path (CLI/API) must reject junk before it reaches the DB —
    unlike `priority_rank`, this raises."""
    import pytest

    with pytest.raises(ValueError, match="unknown priority"):
        normalise_priority("urgent")


def test_task_from_row_defaults_null_priority_to_medium():
    t = Task.new("legacy row", repo_path="/tmp/x")
    row = t.to_row()
    row["priority"] = None
    restored = Task.from_row(row)
    assert restored.priority == DEFAULT_PRIORITY


def test_task_priority_round_trips_through_row():
    t = Task.new("has a priority", repo_path="/tmp/x")
    t.priority = "high"
    restored = Task.from_row(t.to_row())
    assert restored.priority == "high"
