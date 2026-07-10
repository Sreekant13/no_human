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
