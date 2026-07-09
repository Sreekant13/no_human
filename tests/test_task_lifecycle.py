"""Tests for `nh task pause/resume/cancel/retry` and `nh config show`.

CLI commands call asyncio.run() internally, so tests must be synchronous.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from no_human.cli.commands import cli
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _seed_task(db_path: Path, status: TaskStatus, *, title="Test task") -> str:
    async def _go():
        async with Store(db_path) as s:
            t = Task.new(title, repo_path="/tmp/repo")
            await s.create_task(t)
            await s.set_status(t, status, validate=False)
            return t.id
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

    _Cfg.db_path = path

    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda: None)
    return CliRunner()


# --------------------------------------------------------------------------- #
# nh task pause                                                                #
# --------------------------------------------------------------------------- #


def test_pause_active_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "pause", task_id], catch_exceptions=False)
    assert result.exit_code == 0
    assert "paused" in result.output
    t = _get_task(db, task_id)
    assert t.status == TaskStatus.BLOCKED
    assert t.blocker["category"] == "USER_PAUSED"


def test_pause_already_parked(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.BLOCKED)
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "pause", task_id], catch_exceptions=False)
    assert result.exit_code == 0
    assert "cannot pause" in result.output


def test_pause_done_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "pause", task_id], catch_exceptions=False)
    assert result.exit_code == 0
    assert "cannot pause" in result.output


def test_pause_unknown_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, TaskStatus.IMPLEMENTING)  # ensure DB exists
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "pause", "nonexistent"], catch_exceptions=False)
    assert result.exit_code != 0


# --------------------------------------------------------------------------- #
# nh task resume                                                               #
# --------------------------------------------------------------------------- #


def test_resume_blocked_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.BLOCKED)
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "resume", task_id], catch_exceptions=False)
    assert result.exit_code == 0
    assert "resumed" in result.output
    t = _get_task(db, task_id)
    assert t.status == TaskStatus.IMPLEMENTING
    assert t.blocker is None


def test_resume_active_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "resume", task_id], catch_exceptions=False)
    assert result.exit_code == 0
    assert "only parked tasks" in result.output


# --------------------------------------------------------------------------- #
# nh task cancel                                                               #
# --------------------------------------------------------------------------- #


def test_cancel_active_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "cancel", task_id], catch_exceptions=False)
    assert result.exit_code == 0
    assert "cancelled" in result.output
    t = _get_task(db, task_id)
    assert t.status == TaskStatus.FAILED
    assert t.context["cancel_reason"] == "cancelled by user"


def test_cancel_with_reason(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(
        cli, ["task", "cancel", task_id, "--reason", "no longer needed"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    t = _get_task(db, task_id)
    assert t.context["cancel_reason"] == "no longer needed"


def test_cancel_already_done(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.DONE)
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "cancel", task_id], catch_exceptions=False)
    assert result.exit_code == 0
    assert "already done" in result.output


# --------------------------------------------------------------------------- #
# nh task retry                                                                #
# --------------------------------------------------------------------------- #


def test_retry_failed_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.FAILED)
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "retry", task_id], catch_exceptions=False)
    assert result.exit_code == 0
    assert "retried" in result.output
    t = _get_task(db, task_id)
    assert t.status == TaskStatus.PENDING
    assert "retried_at" in t.context


def test_retry_active_task_rejected(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "retry", task_id], catch_exceptions=False)
    assert result.exit_code == 0
    assert "only failed tasks" in result.output
    t = _get_task(db, task_id)
    assert t.status == TaskStatus.IMPLEMENTING  # unchanged


# --------------------------------------------------------------------------- #
# nh config show                                                               #
# --------------------------------------------------------------------------- #


def test_config_show(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"git": {"never_push_to": ["main"]}}))

    import no_human.cli.commands as cmd_mod
    import no_human.config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
    # The config command imports CONFIG_PATH from config module at call time

    runner = CliRunner()
    result = runner.invoke(cli, ["config", "show"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "never_push_to" in result.output


def test_config_show_key(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"git": {"never_push_to": ["main", "master"]}}))

    import no_human.config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["config", "show", "--key", "git"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "never_push_to" in result.output


def test_config_path(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.touch()

    import no_human.config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["config", "path"], catch_exceptions=False)
    assert result.exit_code == 0
    # Rich may wrap the long path; check the filename appears.
    assert "config.yaml" in result.output


# --------------------------------------------------------------------------- #
# nh task add — linked-repo validation (D19)                                   #
# --------------------------------------------------------------------------- #


def test_task_add_rejects_a_linked_repo_that_is_not_a_checkout(tmp_path, monkeypatch):
    """D19: click already rejects a *missing* --linked-repo (Path(exists=True)).
    A path that exists but is not a git checkout used to sail through intake and
    then be dropped by a bare `continue` mid-attempt, after the planner had
    already written a plan naming its files."""
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)
    primary = tmp_path / "primary"
    (primary / ".git").mkdir(parents=True)
    not_a_checkout = tmp_path / "metrics-core-service"
    not_a_checkout.mkdir()

    result = runner.invoke(cli, [
        "task", "add", "--title", "multi-repo task",
        "--repo", str(primary),
        "--linked-repo", str(not_a_checkout),
        "--no-grill", "--no-run",
    ], catch_exceptions=False)

    assert result.exit_code == 1
    assert "not a git repo" in result.output
    assert "metrics-core-service" in result.output
