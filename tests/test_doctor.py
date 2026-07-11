"""Liveness diagnostics: the silences must be enumerable.

Every contradiction rule in doctor.py is a silent death the project really
had; these tests pin each one to a synthetic DB that reproduces it.
"""

from __future__ import annotations

import time

import pytest

from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.doctor import MECHANISMS, diagnose


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


def _ev(kind: str, **extra) -> dict:
    return {"source": "test", "kind": kind, "text": "", "ts": time.time(), **extra}


async def test_empty_db_reports_all_mechanisms_as_never_fired(store):
    d = await diagnose(store)
    assert len(d.mechanisms) == len(MECHANISMS)
    assert all(m["count"] == 0 and m["hint"] for m in d.mechanisms)
    assert d.healthy, "an empty install has nothing to contradict"


async def test_the_testing_dead_pattern_is_a_contradiction(store):
    """Reviews ran while tests never did — unnoticed for the system's life."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [_ev("review"), _ev("review")])
    d = await diagnose(store)
    assert any("TESTS NEVER RAN" in c for c in d.contradictions)
    assert not d.healthy


async def test_the_silent_watcher_pattern_is_a_contradiction(store):
    """A task parked at awaiting_approval with zero watcher events ever."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [_ev("pr_open"), _ev("review"), _ev("tests")])
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    d = await diagnose(store)
    assert any("WATCHER SILENT" in c for c in d.contradictions)
    # One fresh persisted watcher event clears it.
    await store.save_events(t.id, [_ev("pr_feedback_skipped", source="watcher")])
    d = await diagnose(store)
    assert not any("WATCHER" in c for c in d.contradictions)


async def test_stale_watcher_evidence_is_a_contradiction(store):
    """Heartbeats are hourly; a parked task whose newest watcher evidence is
    hours old means the watcher stopped ticking after it last acted."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [
        _ev("pr_open"), _ev("review"), _ev("tests"),
        {**_ev("wake_tick", source="watcher"), "ts": time.time() - 10 * 3600},
    ])
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    d = await diagnose(store)
    assert any("WATCHER STALE" in c for c in d.contradictions)


async def test_a_status_without_its_evidence_is_a_gap(store):
    """awaiting_approval with no pr_open event = a signal that lies."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    d = await diagnose(store)
    assert any(t.id[:8] in g and "pr_open" in g for g in d.evidence_gaps)
    await store.save_events(t.id, [_ev("pr_open")])
    d = await diagnose(store)
    assert not d.evidence_gaps


async def test_an_escalation_with_an_empty_blocker_is_a_gap(store):
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.ESCALATED, validate=False)
    d = await diagnose(store)
    assert any("empty blocker" in g for g in d.evidence_gaps)
    t.blocker = {"question": "Spend more, or stop here?"}
    await store.update_task(t)
    d = await diagnose(store)
    assert not any("empty blocker" in g for g in d.evidence_gaps)


async def test_unreviewed_pr_is_a_contradiction(store):
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [_ev("pr_open"), _ev("tests")])
    d = await diagnose(store)
    assert any("UNREVIEWED" in c for c in d.contradictions)


async def test_ci_gate_triggered_but_never_passed_on_a_done_task_contradicts(store):
    """M6: a done task whose CI_GATE integration run started and never went
    green is a verdict without its evidence."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [
        _ev("pr_open"), _ev("review"), _ev("tests"),
        _ev("ci_gate_trigger"),
    ])
    await store.set_status(t, TaskStatus.DONE, validate=False)
    d = await diagnose(store)
    assert any("CI_GATE UNPROVEN" in c for c in d.contradictions)
    # The pass event clears it.
    await store.save_events(t.id, [_ev("ci_gate_pass")])
    d = await diagnose(store)
    assert not any("CI_GATE UNPROVEN" in c for c in d.contradictions)


async def test_ci_gate_integration_is_an_enumerated_mechanism(store):
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [_ev("ci_gate_trigger"), _ev("ci_gate_pass")])
    d = await diagnose(store)
    m = next(m for m in d.mechanisms if m["name"] == "ci_gate_integration")
    assert m["count"] == 2


async def test_spurious_budget_escalation_after_ci_gate_pass_contradicts(store):
    """The 2026-07-10 shape: validation passed, no new coder work, yet the
    task sits escalated BUDGET_EXHAUSTED — a resume fired on a non-human
    trigger."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [
        _ev("pr_open"), _ev("attempt_start"), _ev("ci_gate_pass"),
    ])
    t.blocker = {"category": "BUDGET_EXHAUSTED", "question": "raise?"}
    await store.update_task(t)
    await store.set_status(t, TaskStatus.ESCALATED, validate=False)
    d = await diagnose(store)
    assert any("SPURIOUS ESCALATION" in c for c in d.contradictions)
    # Real coder work AFTER the pass = a legitimate escalation — no flag.
    await store.save_events(t.id, [_ev("attempt_start")])
    d = await diagnose(store)
    assert not any("SPURIOUS ESCALATION" in c for c in d.contradictions)


async def test_orphaned_worktree_is_a_contradiction(store, tmp_path, monkeypatch):
    """W2.6: a crashed run's worktree lingers invisibly until the next acquire
    fails or the disk fills. A worktree whose task is KNOWN to this store but
    inactive (failed/done) is an orphan; one owned by a running task is not;
    one whose id is unknown to this store belongs to a different install and
    must NOT be flagged (that false positive broke the empty-DB doctor test)."""
    fake_home = tmp_path / ".no_human"
    (fake_home / "worktrees" / "deadbeef1234").mkdir(parents=True)
    monkeypatch.setattr("no_human.config.NO_HUMAN_HOME", fake_home)

    # Unknown to this store → NOT flagged (different install / isolated test).
    d = await diagnose(store)
    assert not any("ORPHANED WORKTREE" in c for c in d.contradictions)

    # A known but FAILED task with a lingering worktree → orphan.
    t = Task.new("crashed", repo_path="/tmp/x")
    t.id = "deadbeef1234"
    await store.create_task(t)
    await store.set_status(t, TaskStatus.FAILED, validate=False)
    d = await diagnose(store)
    assert any("ORPHANED WORKTREE" in c and "deadbeef1234" in c
               for c in d.contradictions)

    # The same worktree owned by an actively-implementing task: not an orphan.
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
    d = await diagnose(store)
    assert not any("ORPHANED WORKTREE" in c for c in d.contradictions)


async def test_done_code_review_needs_no_pr_open(store):
    """A standalone code-review finishes with cited comments, not a PR — 'done'
    without pr_open must NOT be flagged as an evidence gap for it (false positive
    that flagged f71107e9 every run). A done FEATURE task still must have one."""
    cr = Task.new("review PR 123", repo_path="/tmp/x")
    cr.kind = "code_review"
    await store.create_task(cr)
    await store.set_status(cr, TaskStatus.DONE, validate=False)
    d = await diagnose(store)
    assert not any(cr.id[:8] in g and "pr_open" in g for g in d.evidence_gaps)

    feat = Task.new("add feature", repo_path="/tmp/x")
    feat.kind = "feature"
    await store.create_task(feat)
    await store.set_status(feat, TaskStatus.DONE, validate=False)
    d = await diagnose(store)
    assert any(feat.id[:8] in g and "pr_open" in g for g in d.evidence_gaps)
