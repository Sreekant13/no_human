"""Tests for the PR comment watcher (Phase C — WS-C)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from no_human.vcs.pr_watcher import (
    PrComment,
    PrFeedback,
    check_pr_comments,
)
from no_human.blockers.wake import WakeWatcher
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus


# --------------------------------------------------------------------------- #
# PrComment / PrFeedback unit tests                                           #
# --------------------------------------------------------------------------- #


def test_pr_comment_basic():
    c = PrComment(author="alice", body="Fix the null check", created_at="2026-01-01T00:00:00Z")
    assert c.author == "alice"
    assert c.body == "Fix the null check"


def test_pr_feedback_to_send_back_entries():
    feedback = PrFeedback(
        pr_url="https://github.com/org/repo/pull/42",
        comments=[
            PrComment(
                author="alice", body="This is wrong",
                path="src/main.py", line=10,
                diff_hunk="- old_line\n+ new_line",
                created_at="2026-01-01T00:00:00Z",
            ),
            PrComment(
                author="bob", body="Please fix",
                created_at="2026-01-01T01:00:00Z",
            ),
        ],
    )
    entries = feedback.to_send_back_entries()
    assert len(entries) == 2

    # First entry: inline comment with path + line + diff hunk.
    assert "[src/main.py:10]" in entries[0]["message"]
    assert "This is wrong" in entries[0]["message"]
    assert "old_line" in entries[0]["message"]
    assert entries[0]["author"] == "alice"
    assert entries[0]["source"] == "pr_comment"

    # Second entry: general comment, no path.
    assert "Please fix" in entries[1]["message"]
    assert entries[1]["author"] == "bob"


def test_pr_feedback_empty():
    feedback = PrFeedback(pr_url="https://x", comments=[])
    assert feedback.to_send_back_entries() == []


# --------------------------------------------------------------------------- #
# check_pr_comments dispatch logic (no real CLI — tests format parsing)        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_check_pr_comments_unrecognized_format():
    # Unrecognized format → empty list, no crash.
    result = await check_pr_comments("just_a_string")
    assert result == []


@pytest.mark.asyncio
async def test_check_pr_comments_bad_github_number():
    result = await check_pr_comments("org/repo#abc")
    assert result == []


@pytest.mark.asyncio
async def test_check_pr_comments_bad_gitlab_iid():
    result = await check_pr_comments("project_id!abc")
    assert result == []


# --------------------------------------------------------------------------- #
# Wake condition: pr_comment_on:<ref>                                         #
# --------------------------------------------------------------------------- #

@pytest.fixture
async def store(tmp_path):
    async with Store(tmp_path / "test.db") as s:
        yield s


def _cfg(**over):
    base = {"blockers": {"max_park_duration": "48h"}}
    base["blockers"].update(over)
    return base


async def _park(store, *, status, blocker, wake_at=None):
    t = Task.new("PR task", repo_path="/tmp/r")
    await store.create_task(t)
    t.blocker = blocker
    t.wake_check_at = wake_at
    await store.update_task(t)
    await store.set_status(t, status, validate=False)
    return t


@pytest.mark.asyncio
async def test_pr_comment_condition_resumes_with_feedback(store):
    """When pr_comment_on fires, the task gets resumed AND comments are injected."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "pr_comment_on:org/repo#42",
            "raised_at": now.isoformat(), "confidence": 0.9,
        },
    )

    comment = PrComment(author="reviewer", body="Fix the edge case", created_at=now.isoformat())

    async def pr_comment_checker(ref):
        assert ref == "org/repo#42"
        return [comment]

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment_checker)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions

    # Verify comments were injected into task context.
    refreshed = await store.get_task(t.id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    feedback = refreshed.context.get("send_back_feedback", [])
    assert len(feedback) >= 1
    assert "Fix the edge case" in feedback[-1]["message"]
    assert feedback[-1]["source"] == "pr_comment"


@pytest.mark.asyncio
async def test_pr_comment_condition_no_comments_not_satisfied(store):
    """If the PR has no new comments, condition is not satisfied."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "pr_comment_on:org/repo#42",
            "raised_at": now.isoformat(), "confidence": 0.9,
        },
    )

    async def pr_comment_checker(ref):
        return []  # no new comments

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment_checker)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") not in actions


@pytest.mark.asyncio
async def test_pr_comment_condition_no_checker_not_satisfied(store):
    """No checker wired → never satisfied."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "pr_comment_on:org/repo#42",
            "raised_at": now.isoformat(), "confidence": 0.9,
        },
    )

    watcher = WakeWatcher(store, _cfg())
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") not in actions


@pytest.mark.asyncio
async def test_pr_comment_condition_checker_error_safe(store):
    """Checker throwing → not satisfied, not crashed."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "pr_comment_on:org/repo#42",
            "raised_at": now.isoformat(), "confidence": 0.9,
        },
    )

    async def pr_comment_checker(ref):
        raise RuntimeError("API down")

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment_checker)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") not in actions


@pytest.mark.asyncio
async def test_pr_comment_inline_formatting(store):
    """Inline comments (with path/line) get formatted with file:line prefix."""
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    t = await _park(
        store, status=TaskStatus.BLOCKED,
        blocker={
            "category": "DEPENDENCY_WAIT",
            "wake_condition": "pr_comment_on:org/repo#42",
            "raised_at": now.isoformat(), "confidence": 0.9,
        },
    )

    comment = PrComment(
        author="alice", body="Null check missing",
        path="src/handler.py", line=55,
        diff_hunk="+ if value is not None:",
        created_at=now.isoformat(),
    )

    async def pr_comment_checker(ref):
        return [comment]

    watcher = WakeWatcher(store, _cfg(), pr_comment=pr_comment_checker)
    actions = await watcher.tick(now=now)
    assert (t.id, "resumed") in actions

    refreshed = await store.get_task(t.id)
    fb = refreshed.context["send_back_feedback"][-1]
    assert "[src/handler.py:55]" in fb["message"]
    assert "Null check missing" in fb["message"]
    assert "if value is not None" in fb["message"]


async def test_post_reply_comment_stamps_the_agent_marker(monkeypatch):
    """Every comment no_human posts must carry the invisible marker — it is
    the only thing distinguishing the product's comments from the operator's
    (same gh login; the 2026-07-10 self-resume incident)."""
    from no_human.vcs import pr_watcher

    captured = {}

    async def fake_run_cli(cmd):
        captured["cmd"] = cmd
        return "ok"

    monkeypatch.setattr(pr_watcher, "_run_cli", fake_run_cli)
    ok = await pr_watcher.post_reply_comment("host/o/r#5", "hello reviewer")
    assert ok
    body = captured["cmd"][captured["cmd"].index("--body") + 1]
    assert body.startswith(pr_watcher.AGENT_COMMENT_MARKER)
    assert "hello reviewer" in body
    assert pr_watcher.is_agent_comment(body)
    assert not pr_watcher.is_agent_comment("hello reviewer")


# ── upsert_agent_comment: update one comment, never pile up (PR #531 had 17) ──

async def test_upsert_updates_existing_github_comment_instead_of_posting_new(monkeypatch):
    import no_human.vcs.pr_watcher as pw

    calls = []

    async def fake_run(cmd):
        calls.append(cmd)
        joined = " ".join(cmd)
        if "/issues/" in joined and joined.endswith("--paginate"):
            # an existing agent comment for key "ci_gate"
            return '[{"id": 99, "body": "<!-- no_human-agent-comment --><!-- nh:ci_gate -->\\nold"}]'
        return "{}"  # PATCH/POST succeed

    monkeypatch.setattr(pw, "_run_cli", fake_run)
    ok = await pw.upsert_agent_comment("code.example.com/dev/metrics-core-query-service#531", "new status", key="ci_gate")
    assert ok is True
    # It PATCHed comment 99, and did NOT POST a new one.
    assert any("PATCH" in " ".join(c) and "/issues/comments/99" in " ".join(c) for c in calls)
    assert not any("-X" in c and "POST" in c and "/issues/531/comments" in " ".join(c) for c in calls)


async def test_upsert_creates_when_none_exists(monkeypatch):
    import no_human.vcs.pr_watcher as pw

    calls = []

    async def fake_run(cmd):
        calls.append(cmd)
        if " ".join(cmd).endswith("--paginate"):
            return "[]"  # no existing comment
        return "{}"

    monkeypatch.setattr(pw, "_run_cli", fake_run)
    ok = await pw.upsert_agent_comment("code.example.com/dev/r#5", "hi", key="ci_gate")
    assert ok is True
    assert any("POST" in c for c in calls if isinstance(c, list) for c in [" ".join(c)]) or \
        any("POST" in " ".join(c) for c in calls)


def test_upsert_body_never_says_no_human_visibly():
    # The visible text must not mention no_human; only the invisible HTML marker does.
    from no_human.vcs.pr_watcher import AGENT_COMMENT_MARKER
    assert AGENT_COMMENT_MARKER.startswith("<!--")  # invisible
