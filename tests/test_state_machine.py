"""Task state-machine transition rules (PLAN.md 4.3)."""

import pytest

from no_human.core.task import (
    IllegalTransition,
    Task,
    TaskStatus,
    assert_transition,
    can_transition,
)

S = TaskStatus


def test_happy_path_progression():
    flow = [
        S.PENDING, S.CONTEXT, S.PLANNING, S.IMPLEMENTING,
        S.REVIEWING, S.TESTING, S.AWAITING_APPROVAL, S.DONE,
    ]
    for src, dst in zip(flow, flow[1:]):
        assert can_transition(src, dst), f"{src}->{dst} should be allowed"


def test_cannot_skip_states():
    assert not can_transition(S.PENDING, S.IMPLEMENTING)
    assert not can_transition(S.CONTEXT, S.TESTING)
    with pytest.raises(IllegalTransition):
        assert_transition(S.PENDING, S.DONE)


def test_active_states_can_drop_to_offramps():
    for off in (S.BLOCKED, S.AWAITING_INPUT, S.PAUSED_QUOTA, S.ESCALATED, S.FAILED):
        assert can_transition(S.IMPLEMENTING, off)
        assert can_transition(S.TESTING, off)


def test_review_and_test_can_loop_back_to_implement():
    assert can_transition(S.REVIEWING, S.IMPLEMENTING)
    assert can_transition(S.TESTING, S.IMPLEMENTING)


def test_approval_routes():
    assert can_transition(S.AWAITING_APPROVAL, S.DONE)        # approved
    assert can_transition(S.AWAITING_APPROVAL, S.IMPLEMENTING)  # sent back


def test_parked_states_resume_to_active():
    for parked in (S.BLOCKED, S.AWAITING_INPUT, S.PAUSED_QUOTA, S.ESCALATED):
        assert can_transition(parked, S.IMPLEMENTING)


def test_failed_is_terminal():
    for s in S:
        if s is S.FAILED:
            continue
        assert not can_transition(S.FAILED, s), f"FAILED->{s} must be blocked"


def test_done_is_terminal():
    for s in S:
        if s is S.DONE:
            continue
        assert not can_transition(S.DONE, s)


def test_same_state_is_noop_allowed():
    assert can_transition(S.IMPLEMENTING, S.IMPLEMENTING)


def test_task_roundtrip_serialization():
    t = Task.new("fix the thing", repo_path="/tmp/r", description="d")
    t.acceptance_criteria = ["a", "b"]
    t.status = S.TESTING
    row = t.to_row()
    back = Task.from_row(row)
    assert back.id == t.id
    assert back.acceptance_criteria == ["a", "b"]
    assert back.status is S.TESTING
