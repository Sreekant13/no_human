"""Tests for `nh task pause/resume/cancel/retry` and `nh config show`.

CLI commands call asyncio.run() internally, so tests must be synchronous.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from no_human.cli.commands import cli
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.profile import ProjectProfile


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
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    # `_server_owns_worker` really does HTTP to 127.0.0.1:8420, so pause/cancel
    # would branch on whether a server happens to be running on the developer's
    # machine — green with none up, red with one. Default to "no server"; the
    # tests about the handover set it True themselves.
    monkeypatch.setattr(cmd_mod, "_server_owns_worker", lambda _cfg: False)
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
    already written a plan naming its files.

    The console width is pinned because it decides whether this test is testing
    anything. Rich falls back to 80 columns whenever stdout is not a terminal —
    a pipe, a log file, every CI runner — and its default rendering folds a
    token longer than the line, so the reported path came back with a newline
    inside it. Unpinned, tmp_path's length alone decides where that fold lands
    and therefore whether the assertion below notices: this test passed on
    macOS and failed on Linux for no reason other than that.
    """
    monkeypatch.setenv("COLUMNS", "80")
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)
    primary = tmp_path / "primary"
    (primary / ".git").mkdir(parents=True)
    not_a_checkout = tmp_path / "metrics-core-service"
    not_a_checkout.mkdir()
    # pytest's tmp_path is always well past 80 characters, so the rendered
    # message cannot fit on one 80-column line — the path has to survive being
    # wrapped, which is the point.
    assert len(str(not_a_checkout)) > 80

    result = runner.invoke(cli, [
        "task", "add", "--title", "multi-repo task",
        "--repo", str(primary),
        "--linked-repo", str(not_a_checkout),
        "--no-grill", "--no-run",
    ], catch_exceptions=False)

    assert result.exit_code == 1
    assert "not a git repo" in result.output
    assert "metrics-core-service" in result.output
    # The whole path, verbatim and unbroken — a path the user cannot copy out
    # of the error is not a usable error message.
    assert str(not_a_checkout) in result.output


# --------------------------------------------------------------------------- #
# nh task add — repo default budgets (SCRUM-48)                                #
# --------------------------------------------------------------------------- #

def _created_task_id(output: str) -> str:
    match = re.search(r"created task ([0-9a-f]{8})", output)
    assert match, f"no 'created task <id>' line in output:\n{output}"
    return match.group(1)


def test_task_add_applies_repo_default_budgets(tmp_path, monkeypatch):
    """SCRUM-48: `nh task add` must merge repo default budgets in, same as
    the web create path (api/app.py) and the Jira poller already do."""
    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    repo.mkdir()

    async def _seed():
        async with Store(db) as s:
            await s.upsert_profile(ProjectProfile(
                repo_path=str(repo.resolve()),
                default_attempt_tokens=6_000_000,
                default_lifetime_tokens=16_000_000,
            ))
    asyncio.run(_seed())

    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, [
        "task", "add", "--title", "Heavy repo task",
        "--repo", str(repo), "--no-grill", "--no-run",
    ], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    task_id = _created_task_id(result.output)
    task = _get_task(db, task_id)
    assert task.config["attempt_tokens"] == 6_000_000
    assert task.config["lifetime_tokens"] == 16_000_000


def test_task_add_explicit_config_overrides_repo_defaults(tmp_path, monkeypatch):
    """An explicit key already on the task's config by the time `task add`
    creates it (e.g. a future explicit-budget flag) must survive the
    repo-defaults merge untouched, while an unset key still gets filled in."""
    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    repo.mkdir()

    async def _seed():
        async with Store(db) as s:
            await s.upsert_profile(ProjectProfile(
                repo_path=str(repo.resolve()),
                default_attempt_tokens=6_000_000,
                default_lifetime_tokens=16_000_000,
            ))
    asyncio.run(_seed())

    from no_human.core.task import Task as TaskCls
    orig_new = TaskCls.new

    def _new_with_explicit_override(*a, **kw):
        t = orig_new(*a, **kw)
        t.config["attempt_tokens"] = 999
        return t

    monkeypatch.setattr(TaskCls, "new", _new_with_explicit_override)

    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, [
        "task", "add", "--title", "Override task",
        "--repo", str(repo), "--no-grill", "--no-run",
    ], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    task_id = _created_task_id(result.output)
    task = _get_task(db, task_id)
    assert task.config["attempt_tokens"] == 999            # explicit wins, not clobbered
    assert task.config["lifetime_tokens"] == 16_000_000    # untouched key still gets the default


# --------------------------------------------------------------------------- #
# Duplicate execution: the server's scheduler already claims the task          #
# --------------------------------------------------------------------------- #


def test_server_owns_worker_is_false_when_nothing_is_listening():
    """Any failure to reach the server means "no server" — a false positive would
    silently strand the task, a false negative only restores the old behavior."""
    import no_human.cli.commands as cmd_mod

    class _Cfg(dict):
        def get(self, k, d=None):
            return {"server": {"host": "127.0.0.1", "port": 1}}.get(k, d)

    assert cmd_mod._server_owns_worker(_Cfg()) is False


def test_reply_does_not_run_the_task_when_the_server_is_up(tmp_path, monkeypatch):
    """`nh reply` sets the task to IMPLEMENTING, which scheduler._CLAIMABLE picks
    up. Running it in-process too gave task 84251cb2 two orchestrators on one git
    checkout — duplicate commit/reviewing events and a doubled escalation."""
    import no_human.cli.commands as cmd_mod

    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.BLOCKED)
    runner = _make_runner(db, monkeypatch)

    monkeypatch.setattr(cmd_mod, "_server_owns_worker", lambda _cfg: True)
    ran: list[str] = []
    monkeypatch.setattr(cmd_mod, "_build_orchestrator",
                        lambda *a, **k: ran.append("built") or (_ for _ in ()).throw(
                            AssertionError("orchestrator must not run in-process")))

    result = runner.invoke(cli, ["reply", task_id, "go on"], catch_exceptions=False)

    assert result.exit_code == 0
    assert ran == [], "the CLI ran the task while the server owned it"
    assert "picked it up" in result.output


def _cancel_flag(db_path: Path, task_id: str) -> str | None:
    async def _go():
        async with Store(db_path) as s:
            return await s.get_cancel_request(task_id)
    return asyncio.run(_go())


def test_pause_defers_the_status_to_the_running_server(tmp_path, monkeypatch):
    """With a server up, the orchestrator owns the task's status. The CLI raises
    the stop flag and stops there; writing BLOCKED from here would race the
    attempt that is still running and lose its [WIP-BLOCKED] checkpoint."""
    import no_human.cli.commands as cmd_mod

    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)
    monkeypatch.setattr(cmd_mod, "_server_owns_worker", lambda _cfg: True)

    result = runner.invoke(cli, ["task", "pause", task_id], catch_exceptions=False)

    assert result.exit_code == 0
    assert _cancel_flag(db, task_id) == "user paused via CLI"
    assert _get_task(db, task_id).status is TaskStatus.IMPLEMENTING


def test_pause_without_a_server_parks_the_task_itself(tmp_path, monkeypatch):
    """No server means nothing is running, so this process is the only writer:
    park the task and consume the flag in one go."""
    import no_human.cli.commands as cmd_mod

    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.IMPLEMENTING)
    runner = _make_runner(db, monkeypatch)
    monkeypatch.setattr(cmd_mod, "_server_owns_worker", lambda _cfg: False)

    result = runner.invoke(cli, ["task", "pause", task_id], catch_exceptions=False)

    assert result.exit_code == 0
    assert _get_task(db, task_id).status is TaskStatus.BLOCKED
    assert _cancel_flag(db, task_id) is None


def test_resume_withdraws_a_pending_stop(tmp_path, monkeypatch):
    """Otherwise the next attempt honours the stale flag and parks straight back."""
    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.BLOCKED)
    runner = _make_runner(db, monkeypatch)

    async def _flag():
        async with Store(db) as s:
            await s.request_cancel(task_id, "user paused")
    asyncio.run(_flag())

    result = runner.invoke(cli, ["task", "resume", task_id], catch_exceptions=False)

    assert result.exit_code == 0
    assert _cancel_flag(db, task_id) is None
    assert _get_task(db, task_id).status is TaskStatus.IMPLEMENTING


def test_resume_continues_from_the_blockers_checkpoint(tmp_path, monkeypatch):
    """`nh task resume` used to clear the blocker without reading its
    resume_commit, so the next attempt branched from a stale `resume_from` and
    silently discarded the parked attempt's committed work (task 84251cb2,
    attempt 11: a correct pagination fix at 06cd40fc)."""
    from no_human.blockers.taxonomy import Blocker, BlockerCategory

    db = tmp_path / "test.db"
    task_id = _seed_task(db, TaskStatus.ESCALATED)

    async def _park():
        async with Store(db) as s:
            t = await s.find_task(task_id)
            b = Blocker(category=BlockerCategory.NOVEL_UNKNOWN, transient=False,
                        confidence=0.9, goal="g", root_cause_hypothesis="exhausted")
            b.resume_commit = "06cd40fc" * 5
            b.resume_branch = "dev"
            t.blocker = b.to_dict()
            t.context = {"resume_from": {"sha": "0e22fe3d" * 5, "branch": "dev"}}
            await s.update_task(t)
    asyncio.run(_park())

    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "resume", task_id], catch_exceptions=False)

    assert result.exit_code == 0
    t = _get_task(db, task_id)
    assert t.context["resume_from"]["sha"] == "06cd40fc" * 5
    assert t.status is TaskStatus.IMPLEMENTING
