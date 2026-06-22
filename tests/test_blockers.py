"""Tests for the Part 22 blocker taxonomy, escalation report, and wake watcher."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from no_human.blockers import (
    Blocker,
    BlockerCategory,
    WakeWatcher,
    blocker_prompt_suffix,
    fallback_blocker,
    notification_line,
    parse_blocker,
    parse_duration,
    render_report,
    route_for,
    triage,
)
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus


# --------------------------------------------------------------------------- #
# Taxonomy + routing                                                          #
# --------------------------------------------------------------------------- #

def test_category_coerce_aliases():
    assert BlockerCategory.coerce("spec_gap") is BlockerCategory.AMBIGUITY
    assert BlockerCategory.coerce("INVALID") is BlockerCategory.IMPOSSIBLE
    assert BlockerCategory.coerce("rate_limit") is BlockerCategory.TRANSIENT_INFRA
    assert BlockerCategory.coerce("AMBIGUITY/SPEC_GAP") is BlockerCategory.AMBIGUITY


def test_category_coerce_unknown_defaults_to_novel():
    assert BlockerCategory.coerce("banana") is BlockerCategory.NOVEL_UNKNOWN
    assert BlockerCategory.coerce("") is BlockerCategory.NOVEL_UNKNOWN


def test_missing_access_escalates_and_notifies_now():
    r = route_for(BlockerCategory.MISSING_ACCESS)
    assert r.target_status == TaskStatus.ESCALATED
    assert r.notify_now is True
    assert r.parked is False


def test_ambiguity_routes_to_awaiting_input():
    r = route_for(BlockerCategory.AMBIGUITY)
    assert r.target_status == TaskStatus.AWAITING_INPUT
    assert r.notify_now is True


def test_dependency_wait_is_parked_silent():
    r = route_for(BlockerCategory.DEPENDENCY_WAIT)
    assert r.target_status == TaskStatus.BLOCKED
    assert r.notify_now is False
    assert r.parked is True


def test_quota_parks_paused_quota():
    assert route_for(BlockerCategory.QUOTA).target_status == TaskStatus.PAUSED_QUOTA


def test_transient_infra_auto_retry_flag():
    assert route_for(BlockerCategory.TRANSIENT_INFRA).auto_retry is True


# --------------------------------------------------------------------------- #
# Triage with low-confidence override                                         #
# --------------------------------------------------------------------------- #

def test_low_confidence_parkable_escalates_instead_of_parking():
    # DEPENDENCY_WAIT normally parks, but low confidence => ask a human.
    b = Blocker(category=BlockerCategory.DEPENDENCY_WAIT, confidence=0.3,
                wake_condition="pr_merged:org/repo#1")
    route = triage(b, escalate_below_confidence=0.6)
    assert route.target_status == TaskStatus.ESCALATED
    assert route.notify_now is True


def test_high_confidence_parkable_still_parks():
    b = Blocker(category=BlockerCategory.DEPENDENCY_WAIT, confidence=0.9,
                wake_condition="pr_merged:org/repo#1")
    route = triage(b, escalate_below_confidence=0.6)
    assert route.target_status == TaskStatus.BLOCKED


def test_low_confidence_does_not_downgrade_an_escalation():
    # IMPOSSIBLE already escalates; low confidence shouldn't change that.
    b = Blocker(category=BlockerCategory.IMPOSSIBLE, confidence=0.1)
    route = triage(b)
    assert route.target_status == TaskStatus.ESCALATED


# --------------------------------------------------------------------------- #
# Blocker serialization                                                       #
# --------------------------------------------------------------------------- #

def test_blocker_roundtrip():
    b = Blocker(
        category=BlockerCategory.SCOPE_EXPLOSION,
        transient=False,
        confidence=0.8,
        tried=["a", "b"],
        question="Split into 2 tasks?",
        options=["yes", "no"],
        resume_branch="no-human/abc",
        resume_commit="deadbeef",
        goal="implement X",
        evidence="$ cmd\noutput",
    )
    restored = Blocker.from_dict(b.to_dict())
    assert restored.category is BlockerCategory.SCOPE_EXPLOSION
    assert restored.tried == ["a", "b"]
    assert restored.options == ["yes", "no"]
    assert restored.confidence == 0.8


# --------------------------------------------------------------------------- #
# Parsing the agent's structured emission                                     #
# --------------------------------------------------------------------------- #

def test_parse_blocker_from_text():
    text = """
    I cannot proceed without access.
    BLOCKER_JSON_START
    {"category": "MISSING_ACCESS", "confidence": 0.95,
     "question": "Grant repo write?", "root_cause_hypothesis": "token lacks scope"}
    BLOCKER_JSON_END
    """
    b = parse_blocker(text)
    assert b is not None
    assert b.category is BlockerCategory.MISSING_ACCESS
    assert b.question == "Grant repo write?"


def test_parse_blocker_absent_returns_none():
    assert parse_blocker("just some normal output") is None


def test_parse_blocker_malformed_returns_none():
    text = "BLOCKER_JSON_START\n{not valid json}\nBLOCKER_JSON_END"
    assert parse_blocker(text) is None


def test_fallback_blocker_is_novel_unknown():
    b = fallback_blocker("push failed", resume_branch="no-human/x", resume_commit="abc")
    assert b.category is BlockerCategory.NOVEL_UNKNOWN
    assert b.resume_branch == "no-human/x"
    assert b.question is not None


# --------------------------------------------------------------------------- #
# Report rendering (22.4 six-part)                                            #
# --------------------------------------------------------------------------- #

def test_render_report_has_six_sections():
    b = Blocker(
        category=BlockerCategory.AMBIGUITY, confidence=0.7,
        goal="map criterion 3", evidence="$ grep ...\nno match",
        root_cause_hypothesis="criterion 3 is contradictory",
        tried=["interpreted as A: failed", "interpreted as B: failed"],
        question="Which interpretation?", options=["A", "B"],
        resume_branch="no-human/abc123", resume_commit="cafebabe1234",
    )
    out = render_report(b, task_title="Fix login", task_id="abcdef123456")
    for heading in ["## 1. Goal", "## 2. What happened", "## 3. Why blocked",
                    "## 4. What I tried", "## 5. What I need from you",
                    "## 6. State & resume"]:
        assert heading in out
    assert "[1] A" in out and "[2] B" in out
    assert "WIP-BLOCKED" in out
    # Never a numeric self-score gate.
    assert "/10" not in out


def test_notification_line_is_actionable():
    b = Blocker(category=BlockerCategory.MISSING_ACCESS,
                question="Grant write to org/repo?")
    line = notification_line(b, task_title="T", task_id="abcdef12")
    assert "MISSING_ACCESS" in line
    assert "nh reply abcdef12" in line


def test_prompt_suffix_mentions_no_lowering_the_bar():
    s = blocker_prompt_suffix()
    assert "weakening a test" in s.lower() or "weaken" in s.lower()
    assert "BLOCKER_JSON_START" in s
    assert "/10" not in s  # never a numeric gate


# --------------------------------------------------------------------------- #
# Duration parsing                                                            #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,seconds", [
    ("2h", 7200), ("30m", 1800), ("48h", 172800), ("1d", 86400),
    ("90s", 90), ("1h30m", 5400),
])
def test_parse_duration(text, seconds):
    d = parse_duration(text)
    assert d is not None and d.total_seconds() == seconds


def test_parse_duration_invalid():
    assert parse_duration("") is None
    assert parse_duration("soon") is None


# --------------------------------------------------------------------------- #
# Wake watcher                                                                #
# --------------------------------------------------------------------------- #

@pytest_asyncio.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "wake.db").connect()
    yield s
    await s.close()


def _cfg(**over):
    base = {"blockers": {"max_park_duration": "48h"}}
    base["blockers"].update(over)
    return base


async def _park(store, *, status, blocker, updated_offset_hours=0, wake_at=None):
    t = Task.new("Parked task", repo_path="/tmp/r")
    await store.create_task(t)
    t.blocker = blocker
    t.wake_check_at = wake_at
    await store.update_task(t)
    await store.set_status(t, status, validate=False)
    return t


@pytest.mark.asyncio
async def test_after_duration_resumes(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    raised = (now - timedelta(hours=3)).isoformat()
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={"category": "DEPENDENCY_WAIT", "wake_condition": "after:2h",
                 "raised_at": raised, "confidence": 0.9},
    )
    watcher = WakeWatcher(store, _cfg())
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING


@pytest.mark.asyncio
async def test_after_duration_not_yet(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    raised = (now - timedelta(minutes=30)).isoformat()
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={"category": "DEPENDENCY_WAIT", "wake_condition": "after:2h",
                 "raised_at": raised, "confidence": 0.9},
    )
    watcher = WakeWatcher(store, _cfg())
    actions = await watcher.tick(now=now)
    assert actions == []
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.BLOCKED


@pytest.mark.asyncio
async def test_quota_refreshed_resumes_on_wake_check_at(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.PAUSED_QUOTA,
        blocker={"category": "QUOTA", "wake_condition": "quota_refreshed",
                 "raised_at": (now - timedelta(hours=1)).isoformat(), "confidence": 1.0},
        wake_at=(now - timedelta(minutes=1)).isoformat(),
    )
    watcher = WakeWatcher(store, _cfg())
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions


@pytest.mark.asyncio
async def test_ci_green_checker_resumes(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={"category": "DEPENDENCY_WAIT", "wake_condition": "ci_green_on:main",
                 "raised_at": now.isoformat(), "confidence": 0.9},
    )

    async def ci_green(branch):
        return branch == "main"

    watcher = WakeWatcher(store, _cfg(), ci_green=ci_green)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions


@pytest.mark.asyncio
async def test_pr_merged_checker_resumes(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={"category": "DEPENDENCY_WAIT", "wake_condition": "pr_merged:org/repo#7",
                 "raised_at": now.isoformat(), "confidence": 0.9},
    )

    async def pr_merged(ref):
        return ref == "org/repo#7"

    watcher = WakeWatcher(store, _cfg(), pr_merged=pr_merged)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions


@pytest.mark.asyncio
async def test_timeout_escalates(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    raised = (now - timedelta(hours=49)).isoformat()  # past 48h
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={"category": "DEPENDENCY_WAIT", "wake_condition": "pr_merged:org/repo#7",
                 "raised_at": raised, "confidence": 0.9},
    )

    async def pr_merged(ref):
        return False  # never merges

    watcher = WakeWatcher(store, _cfg(), pr_merged=pr_merged)
    actions = await watcher.tick(now=now)
    assert (t.id, "escalated_timeout") in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.ESCALATED
    assert refreshed.blocker["timed_out"] is True


@pytest.mark.asyncio
async def test_awaiting_input_does_not_auto_resume(store):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.AWAITING_INPUT,
        blocker={"category": "AMBIGUITY", "wake_condition": "after:1h",
                 "raised_at": (now - timedelta(hours=2)).isoformat(), "confidence": 0.9},
    )
    watcher = WakeWatcher(store, _cfg())
    actions = await watcher.tick(now=now)
    # Not resumed by time — only a human reply resumes awaiting_input.
    assert (t.id, "resumed") not in actions
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.AWAITING_INPUT
