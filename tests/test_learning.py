"""Tests for the human-confirmed learning queue (PLAN.md 4.5)."""
from __future__ import annotations

import pytest
import pytest_asyncio

from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.learning import LearningQueue, TYPE_ANTI_PATTERN, TYPE_SKILL


@pytest_asyncio.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "learn.db").connect()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def queue(store):
    return LearningQueue(store)


def _task():
    t = Task.new("Fix the parser", repo_path="/tmp/repo")
    return t


@pytest.mark.asyncio
async def test_success_proposes_skill(queue, store):
    mem_id = await queue.propose_from_outcome(
        _task(), status=TaskStatus.AWAITING_APPROVAL, summary="added mul()")
    assert mem_id is not None
    pending = await queue.pending()
    assert len(pending) == 1
    assert pending[0]["type"] == TYPE_SKILL
    assert pending[0]["confirmed"] == 0
    assert pending[0]["source"] == "proposed"


@pytest.mark.asyncio
async def test_failure_proposes_anti_pattern(queue):
    blocker = {
        "category": "NOVEL_UNKNOWN",
        "root_cause_hypothesis": "CI_GATE runner returns X",
        "tried": ["approach A: failed", "approach B: failed"],
    }
    mem_id = await queue.propose_from_outcome(
        _task(), status=TaskStatus.ESCALATED, blocker=blocker)
    assert mem_id is not None
    pending = await queue.pending()
    assert pending[0]["type"] == TYPE_ANTI_PATTERN
    assert "CI_GATE runner returns X" in pending[0]["content"]
    assert "NOVEL_UNKNOWN" in pending[0]["content"]


@pytest.mark.asyncio
async def test_proposal_not_in_active_set_until_confirmed(queue):
    await queue.propose_from_outcome(
        _task(), status=TaskStatus.AWAITING_APPROVAL, summary="x")
    # The active set (what later tasks consult) must NOT include the proposal.
    assert await queue.active() == []
    assert len(await queue.pending()) == 1


@pytest.mark.asyncio
async def test_confirm_promotes_to_active(queue):
    mem_id = await queue.propose_from_outcome(
        _task(), status=TaskStatus.AWAITING_APPROVAL, summary="x")
    assert await queue.confirm(mem_id) is True
    active = await queue.active()
    assert len(active) == 1
    assert active[0]["confirmed"] == 1
    assert active[0]["source"] == "confirmed"
    # No longer pending.
    assert await queue.pending() == []


@pytest.mark.asyncio
async def test_reject_removes_proposal(queue):
    mem_id = await queue.propose_from_outcome(
        _task(), status=TaskStatus.ESCALATED,
        blocker={"category": "IMPOSSIBLE", "root_cause_hypothesis": "no API"})
    assert await queue.reject(mem_id) is True
    assert await queue.pending() == []
    assert await queue.active() == []


@pytest.mark.asyncio
async def test_dedupe_same_lesson(queue):
    """The same lesson proposed twice is deduped (one queue entry)."""
    blocker = {"category": "NOVEL_UNKNOWN", "root_cause_hypothesis": "hit the cap"}
    first = await queue.propose_from_outcome(
        _task(), status=TaskStatus.ESCALATED, blocker=blocker)
    second = await queue.propose_from_outcome(
        _task(), status=TaskStatus.ESCALATED, blocker=blocker)
    assert first is not None
    assert second is None  # deduped
    assert len(await queue.pending()) == 1


@pytest.mark.asyncio
async def test_distinct_lessons_not_deduped(queue):
    a = await queue.propose_from_outcome(
        _task(), status=TaskStatus.ESCALATED,
        blocker={"category": "NOVEL_UNKNOWN", "root_cause_hypothesis": "cap A"})
    b = await queue.propose_from_outcome(
        _task(), status=TaskStatus.ESCALATED,
        blocker={"category": "STAGNATION", "root_cause_hypothesis": "cause B"})
    assert a and b and a != b
    assert len(await queue.pending()) == 2


@pytest.mark.asyncio
async def test_transient_and_resource_blockers_do_not_propose(queue):
    """A budget cap, infra flake, quota wall, dependency wait, or missing-access
    gap is environmental — not a reusable code lesson. Proposing a durable
    anti-pattern for each is what flooded the confirm queue to 197 pending."""
    for cat in ("BUDGET_EXHAUSTED", "TRANSIENT_INFRA", "QUOTA",
                "DEPENDENCY_WAIT", "MISSING_ACCESS"):
        mem_id = await queue.propose_from_outcome(
            _task(), status=TaskStatus.ESCALATED,
            blocker={"category": cat, "root_cause_hypothesis": f"{cat} happened"})
        assert mem_id is None, f"{cat} should not propose a durable learning"
    assert await queue.pending() == []


@pytest.mark.asyncio
async def test_transient_flag_suppresses_proposal(queue):
    """An otherwise-learnable category flagged transient=True is suppressed too."""
    mem_id = await queue.propose_from_outcome(
        _task(), status=TaskStatus.ESCALATED,
        blocker={"category": "NOVEL_UNKNOWN", "transient": True,
                 "root_cause_hypothesis": "a flake"})
    assert mem_id is None


@pytest.mark.asyncio
async def test_parked_blocker_does_not_propose():
    # _build returns None when there is neither success nor a blocker.
    q = LearningQueue.__new__(LearningQueue)  # no store needed for _build
    assert q._build(_task(), status=TaskStatus.BLOCKED, blocker=None, summary="") is None
