"""Tests for `nh task tier <id>` (complexity resourcing observability).

CLI commands drive asyncio.run() internally, so integration tests must be
synchronous — see tests/test_cli_commands.py for the established pattern this
file reuses (_seed_task, _make_runner).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from click.testing import CliRunner

from no_human.cli.commands import cli, format_tier_summary
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _seed_task(db_path: Path, *, context: dict | None = None,
               title="Test task", criteria=None, description="") -> str:
    async def _go():
        async with Store(db_path) as s:
            t = Task.new(title, repo_path="/tmp/repo")
            if criteria is not None:
                t.acceptance_criteria = criteria
            if description:
                t.description = description
            if context is not None:
                t.context = context
            await s.create_task(t)
            await s.set_status(t, TaskStatus.DONE, validate=False)
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
# format_tier_summary — pure, no Store                                        #
# --------------------------------------------------------------------------- #

def test_summary_recorded_complex_shows_all_resourcing():
    out = format_tier_summary(
        "complex", ["multi-repo", "many-criteria"], predicted=False,
        moa_enabled=True,
    )
    assert "complex" in out
    assert "recorded" in out
    assert "multi-repo" in out
    assert "many-criteria" in out
    assert "✓ MoA planning fan-out: applied" in out
    assert "✓ extended thinking: on" in out
    assert "✓ complex-tier angle review passes: applied" in out


def test_summary_predicted_label():
    predicted_out = format_tier_summary("standard", ["long-spec"], predicted=True)
    recorded_out = format_tier_summary("standard", ["long-spec"], predicted=False)
    assert "predicted" in predicted_out
    assert "predicted" not in recorded_out
    assert "recorded" in recorded_out


def test_summary_trivial_no_extras():
    out = format_tier_summary("trivial", [], predicted=False, moa_enabled=True)
    assert "signals: none" in out
    assert "· MoA planning fan-out: not applied" in out
    assert "· extended thinking: off" in out
    assert "· complex-tier angle review passes: not applied" in out


def test_summary_standard_moa_threshold():
    not_applied = format_tier_summary(
        "standard", ["one-signal"], predicted=False,
        moa_min_signals=2, moa_enabled=True,
    )
    applied = format_tier_summary(
        "standard", ["one-signal"], predicted=False,
        moa_min_signals=1, moa_enabled=True,
    )
    assert "· MoA planning fan-out: not applied" in not_applied
    assert "✓ MoA planning fan-out: applied" in applied


def test_summary_moa_disabled_globally_overrides_signals():
    """A complex tier / high signal count must NOT report MoA as applied when
    llm.moa_planning.enabled is False — the outer kill switch in
    orchestrator._generate_plan short-circuits before tier/signals are even
    consulted."""
    out = format_tier_summary(
        "complex", ["multi-repo", "many-criteria"], predicted=False,
        moa_min_signals=1, moa_enabled=False,
    )
    assert "· MoA planning fan-out: not applied (disabled globally" in out
    # extended thinking and angle passes are independent of the MoA switch
    assert "✓ extended thinking: on" in out
    assert "✓ complex-tier angle review passes: applied" in out


def test_summary_unknown_tier_fallback():
    out = format_tier_summary("mystery-tier", [], predicted=False)
    assert "mystery-tier" in out
    assert "unrecognized tier" in out


# --------------------------------------------------------------------------- #
# nh task tier — integration                                                  #
# --------------------------------------------------------------------------- #

def test_tier_command_recorded(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(
        db, context={"complexity_tier": "complex", "complexity_signals": ["multi-repo"]},
    )
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "tier", task_id])

    assert result.exit_code == 0, result.output
    assert "complex" in result.output
    assert "recorded" in result.output
    assert "multi-repo" in result.output


def test_tier_command_predicted_when_not_run(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(db, context={})
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "tier", task_id])

    assert result.exit_code == 0, result.output
    assert "predicted" in result.output


def test_tier_command_unknown_id_exits_nonzero(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, context={})
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "tier", "deadbeef"])

    assert result.exit_code != 0
    assert "no task matching" in result.output.lower()


def test_tier_command_prefix_resolves(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(
        db, context={"complexity_tier": "standard", "complexity_signals": []},
    )
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "tier", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "standard" in result.output


def test_tier_command_is_read_only(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    task_id = _seed_task(
        db, context={"complexity_tier": "simple", "complexity_signals": []},
    )
    before = _get_task(db, task_id)

    async def _attempts():
        async with Store(db) as s:
            return await s.list_attempts(task_id)
    attempts_before = asyncio.run(_attempts())

    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "tier", task_id])
    assert result.exit_code == 0, result.output

    after = _get_task(db, task_id)
    attempts_after = asyncio.run(_attempts())

    assert after.status == before.status
    assert after.context == before.context
    assert attempts_after == attempts_before
