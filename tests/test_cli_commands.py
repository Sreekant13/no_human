"""Tests for the Phase 1 human-action CLI verbs (nh approve / reject / diff / review / logs).

CLI commands call asyncio.run() internally, so tests must be synchronous.
Each helper opens its own fresh Store connection inside asyncio.run() so the
aiosqlite connection is never reused across event loops.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from no_human.cli.commands import cli
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus


# --------------------------------------------------------------------------- #
# Helpers — each opens a fresh Store connection in its own asyncio.run()      #
# --------------------------------------------------------------------------- #

def _seed_task(db_path: Path, status: TaskStatus, *, title="Test task") -> str:
    async def _go():
        async with Store(db_path) as s:
            t = Task.new(title, repo_path="/tmp/repo")
            await s.create_task(t)
            await s.set_status(t, status, validate=False)
            return t.id
    return asyncio.run(_go())


def _seed_attempt(db_path: Path, task_id: str, **fields) -> str:
    async def _go():
        async with Store(db_path) as s:
            aid = await s.create_attempt(task_id, 1)
            if fields:
                await s.update_attempt(aid, **fields)
            return aid
    return asyncio.run(_go())


def _get_task(db_path: Path, task_id: str) -> Task:
    async def _go():
        async with Store(db_path) as s:
            return await s.find_task(task_id)
    return asyncio.run(_go())


def _make_runner(path: Path, monkeypatch) -> CliRunner:
    import no_human.cli.commands as cmd_mod

    class _Cfg:
        primary_model = "claude-sonnet-4-6"
        review_model = "claude-sonnet-4-6"
        data: dict = {}

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]

    _Cfg.db_path = path  # assign after class def — class body can't see enclosing locals

    # Patch where the names are USED (commands.py has `from ..config import load_config`)
    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda: None)
    return CliRunner()


# --------------------------------------------------------------------------- #
# nh approve                                                                   #
# --------------------------------------------------------------------------- #

def test_approve_awaiting_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.AWAITING_APPROVAL)
    _seed_attempt(db, task_id, pr_url="https://example.com/pr/1")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["approve", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "approved" in result.output.lower()
    assert "https://example.com/pr/1" in result.output

    refreshed = _get_task(db, task_id)
    assert refreshed.context.get("approved_at") is not None


def test_approve_wrong_status(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["approve", task_id[:8]])

    assert result.exit_code != 0
    output = result.output.lower()
    assert "not awaiting_approval" in output or "cannot approve" in output


def test_approve_unknown_id(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.PENDING)  # ensure DB exists
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["approve", "deadbeef"])

    assert result.exit_code != 0
    assert "no task" in result.output.lower()


# --------------------------------------------------------------------------- #
# nh reject                                                                    #
# --------------------------------------------------------------------------- #

def test_reject_stores_feedback_and_resets(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.AWAITING_APPROVAL)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reject", task_id[:8], "--reason", "needs better tests"])

    assert result.exit_code == 0, result.output
    assert "sent back" in result.output.lower()

    refreshed = _get_task(db, task_id)
    assert refreshed.status == TaskStatus.IMPLEMENTING
    feedback = refreshed.context.get("send_back_feedback", [])
    assert any("better tests" in f["message"] for f in feedback)


def test_reject_unknown_id(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.PENDING)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["reject", "deadbeef", "--reason", "nope"])

    assert result.exit_code != 0


# --------------------------------------------------------------------------- #
# nh diff                                                                      #
# --------------------------------------------------------------------------- #

def test_diff_no_commit(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["diff", task_id[:8]])

    assert result.exit_code == 0
    assert "no commit" in result.output.lower()


def test_diff_git_failure_handled(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)

    # Override repo_path to a nonexistent dir after seeding
    async def _patch_repo():
        async with Store(db) as s:
            t = await s.find_task(task_id)
            t.repo_path = str(tmp_path / "nonexistent_repo")
            await s.update_task(t)
    asyncio.run(_patch_repo())

    _seed_attempt(db, task_id, commit_sha="abc123def456")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["diff", task_id[:8]])

    # Must not crash — output contains a useful message
    assert result.exit_code == 0
    lower = result.output.lower()
    assert "abc123" in result.output or "git" in lower or "failed" in lower


# --------------------------------------------------------------------------- #
# nh review                                                                    #
# --------------------------------------------------------------------------- #

def test_review_shows_checklist(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)
    checklist = {
        "passed": True,
        "items": [
            {"label": "Tests pass", "passed": True, "evidence": "208 passed"},
            {"label": "No regressions", "passed": True, "evidence": "tamper guard clean"},
        ],
    }
    _seed_attempt(db, task_id, review_checklist=checklist, review_passed=1)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["review", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "Tests pass" in result.output
    assert "208 passed" in result.output
    assert "PASSED" in result.output.upper()


def test_review_no_checklist(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["review", task_id[:8]])

    assert result.exit_code == 0
    assert "no review" in result.output.lower()


# --------------------------------------------------------------------------- #
# nh logs                                                                      #
# --------------------------------------------------------------------------- #

def test_logs_shows_attempts(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.ESCALATED, title="Hard task")
    _seed_attempt(
        db, task_id,
        turns_used=42, tokens_used=15000,
        failure_reason="max_turns exceeded",
        status="failed",
    )
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["logs", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "Hard task" in result.output
    assert "42" in result.output
    assert "max_turns" in result.output


def test_logs_no_attempts(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.PENDING)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["logs", task_id[:8]])

    assert result.exit_code == 0
    assert "no attempts" in result.output.lower()
