"""Task state-machine transition rules (PLAN.md 4.3)."""

import pytest

from no_human.core.task import (
    IllegalTransition,
    LANDED_RECONCILABLE,
    TERMINAL_LANDED_RECONCILABLE,
    Task,
    TaskStatus,
    assert_landed_reconciliation,
    assert_terminal_landed_reconciliation,
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


def test_parked_states_can_resume_to_pending():
    for parked in (S.BLOCKED, S.AWAITING_INPUT, S.PAUSED_QUOTA, S.ESCALATED):
        assert can_transition(parked, S.PENDING)


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


def test_task_parent_id_roundtrip():
    t = Task.new("child task", repo_path="/tmp/r", parent_id="abc123")
    assert t.parent_id == "abc123"
    row = t.to_row()
    assert row["parent_id"] == "abc123"
    back = Task.from_row(row)
    assert back.parent_id == "abc123"


def test_task_parent_id_default_none():
    t = Task.new("standalone task", repo_path="/tmp/r")
    assert t.parent_id is None
    row = t.to_row()
    assert row["parent_id"] is None


def test_review_pass_from_implementing_has_a_path_forward():
    """Incident 6408aba0: a review verdict can land while the row still reads
    IMPLEMENTING (the review runs inside the implement round), so a review
    PASS must have a legal, non-crashing way onward to delivery."""
    assert can_transition(S.IMPLEMENTING, S.TESTING)
    assert can_transition(S.TESTING, S.AWAITING_APPROVAL)


@pytest.mark.parametrize(
    "src,dst",
    [
        # TaskStatus has no "closed" member; DONE and FAILED are its
        # terminal states (task.py TERMINAL_STATES) — these are the
        # closed-equivalent illegal edges.
        (S.DONE, S.IMPLEMENTING),
        (S.DONE, S.TESTING),
        (S.FAILED, S.IMPLEMENTING),
        (S.FAILED, S.TESTING),
        (S.PENDING, S.IMPLEMENTING),
        (S.PENDING, S.DONE),
        (S.CONTEXT, S.TESTING),
        (S.IMPLEMENTING, S.AWAITING_APPROVAL),
        (S.IMPLEMENTING, S.DONE),
        (S.TESTING, S.DONE),
    ],
)
def test_the_guard_was_not_weakened(src, dst):
    """The new IMPLEMENTING->TESTING edge is the ONLY edge added; every other
    previously-illegal transition, including ones adjacent to the new edge,
    must still raise.

    (S.IMPLEMENTING, S.DONE) and (S.TESTING, S.DONE) stay pinned illegal
    HERE, in the general map, on purpose: orphan-landed-reconciliation
    (`Store.reconcile_landed_orphan`) completes those two specific edges
    through its OWN narrower gate, `assert_landed_reconciliation` — see
    `test_landed_reconciliation_edges_are_legal_only_via_the_narrow_gate`
    below — precisely so that widening THIS map never happens. Widening it
    would also legitimize IMPLEMENTING/TESTING->DONE for
    `Orchestrator._advance_after_review`'s plain `set_status(task, target)`
    call, defeating
    `tests/test_post_review_transition_6408aba0.py::
    test_recovery_never_launders_an_illegal_jump`."""
    assert not can_transition(src, dst)
    with pytest.raises(IllegalTransition):
        assert_transition(src, dst)


def test_landed_reconciliation_edges_are_legal_only_via_the_narrow_gate():
    """Orphan recovery must be able to reconcile a landed-but-orphaned row
    straight to DONE from either IMPLEMENTING or TESTING — the two points in
    the flow where `_recover_orphans` finds orphaned rows with a completed
    attempt (PLAN.md item 2/6: orphan recovery landed-work reconciliation) —
    but ONLY through `assert_landed_reconciliation`, the dedicated gate
    `Store.reconcile_landed_orphan` calls, never through the general
    `ALLOWED_TRANSITIONS` map (`can_transition`/`assert_transition`), which
    `test_the_guard_was_not_weakened` above pins as still refusing both
    edges."""
    assert not can_transition(S.IMPLEMENTING, S.DONE)
    assert not can_transition(S.TESTING, S.DONE)
    with pytest.raises(IllegalTransition):
        assert_transition(S.IMPLEMENTING, S.DONE)
    with pytest.raises(IllegalTransition):
        assert_transition(S.TESTING, S.DONE)

    assert S.IMPLEMENTING in LANDED_RECONCILABLE
    assert S.TESTING in LANDED_RECONCILABLE
    assert_landed_reconciliation(S.IMPLEMENTING)  # must not raise
    assert_landed_reconciliation(S.TESTING)  # must not raise


def test_terminal_landed_reconciliation_edge_is_legal_only_via_its_narrow_gate():
    """The TERMINAL-row twin of the test above: a FAILED row (cancelled or
    not — there is no separate CANCELLED status) whose recorded work is
    provably reachable from the base branch is completed to DONE by
    `Store.reconcile_landed_terminal`, but ONLY through
    `assert_terminal_landed_reconciliation` — a separate, narrower gate from
    both `ALLOWED_TRANSITIONS` (still refusing FAILED->DONE, unchanged) and
    `LANDED_RECONCILABLE` (which does NOT gain FAILED; that set stays exactly
    IMPLEMENTING/REVIEWING/TESTING/AWAITING_APPROVAL). Widening either of
    those instead would make FAILED->DONE legal for every plain
    `set_status(task, DONE)` call — exactly the "resurrect a row without
    reachable evidence" failure mode this feature must not introduce."""
    assert not can_transition(S.FAILED, S.DONE)
    with pytest.raises(IllegalTransition):
        assert_transition(S.FAILED, S.DONE)

    assert S.FAILED not in LANDED_RECONCILABLE
    assert S.FAILED in TERMINAL_LANDED_RECONCILABLE
    assert_terminal_landed_reconciliation(S.FAILED)  # must not raise

    for other in (S.CONTEXT, S.IMPLEMENTING):
        assert other not in TERMINAL_LANDED_RECONCILABLE
        with pytest.raises(IllegalTransition):
            assert_terminal_landed_reconciliation(other)


def test_landed_reconciliation_guard_still_refuses_failed_and_early_states():
    """The new edges must not widen into a general DONE bypass: FAILED stays
    terminal (FAILED->DONE is still illegal), and `assert_landed_reconciliation`
    — the narrower gate `Store.reconcile_landed_orphan` calls in addition to
    the transition map — refuses any source status outside
    LANDED_RECONCILABLE, e.g. CONTEXT."""
    assert not can_transition(S.FAILED, S.DONE)
    with pytest.raises(IllegalTransition):
        assert_transition(S.FAILED, S.DONE)

    assert S.CONTEXT not in LANDED_RECONCILABLE
    with pytest.raises(IllegalTransition):
        assert_landed_reconciliation(S.CONTEXT)

    assert S.IMPLEMENTING in LANDED_RECONCILABLE
    assert S.TESTING in LANDED_RECONCILABLE
    assert_landed_reconciliation(S.IMPLEMENTING)  # must not raise
    assert_landed_reconciliation(S.TESTING)  # must not raise
