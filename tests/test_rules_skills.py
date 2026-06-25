"""Tests for `nh rules` and `nh skills` CLI commands."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from click.testing import CliRunner

from no_human.cli.commands import cli
from no_human.core.db import Store


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

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


def _list_memories(db_path: Path, **kwargs):
    async def _go():
        async with Store(db_path) as s:
            return await s.list_memories(**kwargs)
    return asyncio.run(_go())


# --------------------------------------------------------------------------- #
# nh rules                                                                     #
# --------------------------------------------------------------------------- #


def test_rules_add_and_list(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)

    # Add a rule.
    result = runner.invoke(
        cli, ["rules", "add", "--title", "Never auto-merge",
              "--content", "Agent must never merge PRs", "--tag", "safety"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "added" in result.output

    # List rules.
    result = runner.invoke(cli, ["rules", "list"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "Never auto-merge" in result.output

    # Verify in DB — confirmed.
    mems = _list_memories(db, confirmed=True, mem_type="rule")
    assert len(mems) == 1
    assert mems[0]["title"] == "Never auto-merge"
    assert mems[0]["source"] == "manual"


def test_rules_remove(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)

    # Add a rule.
    runner.invoke(
        cli, ["rules", "add", "--title", "Test rule", "--content", "Remove me"],
        catch_exceptions=False,
    )
    mems = _list_memories(db, confirmed=True, mem_type="rule")
    assert len(mems) == 1
    rule_id = mems[0]["id"]

    # Remove by prefix.
    result = runner.invoke(cli, ["rules", "remove", rule_id[:8]], catch_exceptions=False)
    assert result.exit_code == 0
    assert "removed" in result.output

    # Verify gone.
    mems = _list_memories(db, confirmed=True, mem_type="rule")
    assert len(mems) == 0


def test_rules_list_empty(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["rules", "list"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "no confirmed rules" in result.output


# --------------------------------------------------------------------------- #
# nh skills                                                                    #
# --------------------------------------------------------------------------- #


def test_skills_add_and_list(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(
        cli, ["skills", "add", "--title", "Fix flaky tests",
              "--content", "Always add retry logic for timing-sensitive tests",
              "--tag", "testing", "--tag", "reliability"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "added" in result.output

    result = runner.invoke(cli, ["skills", "list"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "Fix flaky tests" in result.output


def test_skills_remove(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)

    runner.invoke(
        cli, ["skills", "add", "--title", "Temp skill", "--content", "Goes away"],
        catch_exceptions=False,
    )
    mems = _list_memories(db, confirmed=True, mem_type="skill")
    assert len(mems) == 1
    skill_id = mems[0]["id"]

    result = runner.invoke(cli, ["skills", "remove", skill_id[:8]], catch_exceptions=False)
    assert result.exit_code == 0
    assert "removed" in result.output


def test_skills_list_empty(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["skills", "list"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "no confirmed skills" in result.output
