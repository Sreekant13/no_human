"""Tests for `nh repo config REPO_PATH KEY=VALUE ...` (SCRUM-26: human-settable
repo profile default token budgets).

CLI commands drive asyncio.run() internally, so integration tests must be
synchronous — see tests/test_task_config_cli.py for the established pattern
this file reuses (_make_runner).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from click.testing import CliRunner

from no_human.cli.commands import cli
from no_human.core.db import Store
from no_human.profile import ProjectProfile


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _get_profile(db_path: Path, repo_path: str) -> ProjectProfile | None:
    async def _go():
        async with Store(db_path) as s:
            return await s.get_profile(repo_path)
    return asyncio.run(_go())


def _seed_profile(db_path: Path, repo_path: str, **fields) -> None:
    async def _go():
        async with Store(db_path) as s:
            await s.upsert_profile(ProjectProfile(repo_path=repo_path, **fields))
    asyncio.run(_go())


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
# nh repo config — integration                                                #
# --------------------------------------------------------------------------- #

def test_repo_config_sets_allowed_keys(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, [
        "repo", "config", str(repo),
        "default_attempt_tokens=6000000", "default_lifetime_tokens=16000000",
    ])

    assert result.exit_code == 0, result.output
    prof = _get_profile(db, str(repo.resolve()))
    assert prof.default_attempt_tokens == 6_000_000
    assert prof.default_lifetime_tokens == 16_000_000


def test_repo_config_round_trip_via_inspect(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = _make_runner(db, monkeypatch)

    set_result = runner.invoke(cli, [
        "repo", "config", str(repo), "default_attempt_tokens=6000000",
    ])
    assert set_result.exit_code == 0, set_result.output

    inspect_result = runner.invoke(cli, ["repo", "config", str(repo)])
    assert inspect_result.exit_code == 0, inspect_result.output
    assert "default_attempt_tokens=6000000" in inspect_result.output
    assert "default_lifetime_tokens=0" in inspect_result.output


def test_repo_config_inspect_no_profile_shows_unset(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["repo", "config", str(repo)])

    assert result.exit_code == 0, result.output
    assert "default_attempt_tokens=0" in result.output
    assert "default_lifetime_tokens=0" in result.output


def test_repo_config_refuses_unknown_key(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_profile(db, str(repo.resolve()))
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["repo", "config", str(repo), "test_cmd=rm -rf /"])

    assert result.exit_code != 0
    assert "default_attempt_tokens" in result.output  # allowed-keys list surfaced
    prof = _get_profile(db, str(repo.resolve()))
    assert prof.default_attempt_tokens == 0


def test_repo_config_rejects_non_positive_and_non_integer(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = _make_runner(db, monkeypatch)

    zero = runner.invoke(cli, ["repo", "config", str(repo), "default_attempt_tokens=0"])
    assert zero.exit_code != 0

    non_int = runner.invoke(cli, ["repo", "config", str(repo), "default_attempt_tokens=lots"])
    assert non_int.exit_code != 0

    assert _get_profile(db, str(repo.resolve())) is None


def test_repo_config_malformed_assignment(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["repo", "config", str(repo), "default_attempt_tokens"])

    assert result.exit_code != 0
    assert _get_profile(db, str(repo.resolve())) is None


def test_repo_config_allows_values_above_global_caps(tmp_path, monkeypatch):
    """Buyer-blocking lesson: repo defaults must be able to exceed the global
    4M attempt / 8M lifetime caps — that's the exact calibration problem this
    feature solves."""
    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, [
        "repo", "config", str(repo),
        "default_attempt_tokens=6000000", "default_lifetime_tokens=16000000",
    ])

    assert result.exit_code == 0, result.output
    prof = _get_profile(db, str(repo.resolve()))
    assert prof.default_attempt_tokens == 6_000_000
    assert prof.default_lifetime_tokens == 16_000_000


def test_repo_config_preserves_existing_profile_fields(tmp_path, monkeypatch):
    """Setting token defaults on an already-onboarded repo must not clobber
    the rest of its profile (ecosystem/test_cmd/confirmed/etc.)."""
    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_profile(
        db, str(repo.resolve()),
        ecosystem="python-pytest", test_cmd="uv run pytest -q", confirmed=True,
    )
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["repo", "config", str(repo), "default_attempt_tokens=6000000"])

    assert result.exit_code == 0, result.output
    prof = _get_profile(db, str(repo.resolve()))
    assert prof.default_attempt_tokens == 6_000_000
    assert prof.ecosystem == "python-pytest"
    assert prof.test_cmd == "uv run pytest -q"
    assert prof.confirmed is True
