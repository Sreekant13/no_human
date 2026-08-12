"""Tests for `nh task config TASK_ID KEY=VALUE ...` (SCRUM-12: human-settable
per-task caps).

CLI commands drive asyncio.run() internally, so integration tests must be
synchronous — see tests/test_task_tier_cli.py for the established pattern
this file reuses (_seed_task, _get_task, _make_runner).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from click.testing import CliRunner

from no_human.cli.commands import cli
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _seed_task(db_path: Path, *, config: dict | None = None,
               title="Test task") -> str:
    async def _go():
        async with Store(db_path) as s:
            t = Task.new(title, repo_path="/tmp/repo")
            if config is not None:
                t.config = config
            await s.create_task(t)
            await s.set_status(t, TaskStatus.DONE, validate=False,
                               event={"source": "test", "kind": "test_seed"})
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
    return CliRunner()


# --------------------------------------------------------------------------- #
# nh task config — integration                                                #
# --------------------------------------------------------------------------- #

def test_config_sets_allowed_override(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "config", task_id, "attempt_tokens=6000000"])

    assert result.exit_code == 0, result.output
    assert "attempt_tokens=6000000" in result.output
    assert _get_task(db, task_id).config["attempt_tokens"] == 6_000_000


def test_config_multiple_keys_atomic(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, [
        "task", "config", task_id,
        "attempt_tokens=6000000", "lifetime_tokens=20000000",
    ])

    assert result.exit_code == 0, result.output
    after = _get_task(db, task_id)
    assert after.config["attempt_tokens"] == 6_000_000
    assert after.config["lifetime_tokens"] == 20_000_000


def test_config_refuses_unknown_key(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "config", task_id, "never_push_to=main"])

    assert result.exit_code != 0
    assert "max_lines_changed" in result.output  # allowed-keys list surfaced
    assert _get_task(db, task_id).config == {}


def test_config_rejects_non_positive_and_non_integer(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db)
    runner = _make_runner(db, monkeypatch)

    zero = runner.invoke(cli, ["task", "config", task_id, "attempt_tokens=0"])
    assert zero.exit_code != 0
    assert _get_task(db, task_id).config == {}

    non_int = runner.invoke(cli, ["task", "config", task_id, "attempt_tokens=lots"])
    assert non_int.exit_code != 0
    assert _get_task(db, task_id).config == {}


def test_config_malformed_assignment(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "config", task_id, "attempt_tokens"])

    assert result.exit_code != 0
    assert _get_task(db, task_id).config == {}


def test_config_lowers_lifetime_tokens_exactly(tmp_path, monkeypatch):
    # SCRUM-44: the human CLI is the gate — it sets the exact requested
    # value, including lowering a runaway cap. Never-lower is a blocker-
    # option-only guard (see tests/test_blockers.py).
    db = tmp_path / "test.db"
    task_id = _seed_task(db, config={"lifetime_tokens": 16_000_000})
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "config", task_id, "lifetime_tokens=8000000"])

    assert result.exit_code == 0, result.output
    assert "kept" not in result.output
    assert _get_task(db, task_id).config["lifetime_tokens"] == 8_000_000


def test_config_lowers_attempt_tokens_exactly(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, config={"attempt_tokens": 1_000_000})
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "config", task_id, "attempt_tokens=500000"])

    assert result.exit_code == 0, result.output
    assert "kept" not in result.output
    assert _get_task(db, task_id).config["attempt_tokens"] == 500_000


def test_config_unknown_task_id_exits_nonzero(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "config", "deadbeef", "attempt_tokens=1000"])

    assert result.exit_code != 0
    assert "no task matching" in result.output.lower()
