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


def test_repo_config_write_stamps_the_post_cutover_unit(tmp_path, monkeypatch):
    """R1: the write path is where the ambiguity is killed. A value typed today
    is WEIGHTED and says so, so it is never re-converted by the read guard."""
    from no_human.core.pricing import WEIGHTED_UNIT

    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, [
        "repo", "config", str(repo), "default_lifetime_tokens=6000000",
    ])
    assert result.exit_code == 0, result.output

    prof = _get_profile(db, str(repo.resolve()))
    assert prof.default_lifetime_tokens == 6_000_000
    assert prof.default_budget_unit == WEIGHTED_UNIT

    # ...and the operator is told the unit and the ratio to the ungranted
    # default AT THE MOMENT OF TYPING, which is the only moment a 5x typo is
    # cheap to catch. Pre-cutover habit types 20,200,000 here.
    assert "cost-weighted" in result.output
    assert "4,000,000" in result.output
    assert "1.5x" in result.output

    inspect_result = runner.invoke(cli, ["repo", "config", str(repo)])
    assert inspect_result.exit_code == 0, inspect_result.output
    assert "default_lifetime_tokens=6000000" in inspect_result.output
    assert f"budget_unit={WEIGHTED_UNIT}" in inspect_result.output


# --------------------------------------------------------------------------- #
# D5: the marker describes the WHOLE profile, so a PARTIAL write cannot claim
# it. `default_budget_unit` is one field for two values; setting it on a
# one-key write silently re-declares the untouched, still-raw sibling as
# weighted, and a stamped value is taken at face value — no floor, no warning.
#
# This is the third instance of one class in this branch (task.config's
# dict-wide marker in blockers/actions.py, then profile.apply_default_task_config,
# now the surface that CREATES the stamp). The rule, stated once: a marker over
# a record may only be written when every value in that record is in the unit
# the marker claims.
# --------------------------------------------------------------------------- #

def _enforced(db_path, repo_path, key):
    """What the gate would actually enforce for `key`, end to end: profile ->
    task.config -> the orchestrator's cap reader, on a stock install."""
    from no_human.core.bounds import Bounds
    from no_human.core.orchestrator import Orchestrator
    from no_human.profile import apply_default_task_config

    cfg = apply_default_task_config(_get_profile(db_path, repo_path), {})
    return Orchestrator._stored_token_cap(
        cfg, key, getattr(Bounds(), key))


def test_a_partial_write_refuses_to_restamp_an_unstamped_sibling(tmp_path, monkeypatch):
    """The DEPLOY NOTE scenario, and the defect it walked the operator into.

    The live profile holds two unstamped pre-cutover RAW values. Correcting
    ONE of them used to exit 0 and re-declare the other as weighted, moving
    the ENFORCED attempt cap from 2,004,850 to 10,100,000 — 5.0x, permanent,
    fail-open, and the output never mentioned the key it re-typed."""
    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    rp = str(repo.resolve())
    _seed_profile(db, rp, default_attempt_tokens=10_100_000,
                  default_lifetime_tokens=20_200_000)
    runner = _make_runner(db, monkeypatch)

    before = _enforced(db, rp, "attempt_tokens")
    assert before == 2_004_850, "the raw value, converted — the honest reading"

    result = runner.invoke(cli, ["repo", "config", rp,
                                 "default_lifetime_tokens=8000000"])

    assert result.exit_code != 0, f"a partial write was accepted: {result.output}"
    # It refuses by NAMING the problem: both keys, and both ways out.
    assert "default_attempt_tokens" in result.output
    assert "default_lifetime_tokens" in result.output
    assert "10,100,000" in result.output or "10100000" in result.output

    # ...and it changed NOTHING. A refusal that half-applied would be worse
    # than the defect it replaces.
    prof = _get_profile(db, rp)
    assert (prof.default_attempt_tokens, prof.default_lifetime_tokens) == (
        10_100_000, 20_200_000)
    assert prof.default_budget_unit == ""
    assert _enforced(db, rp, "attempt_tokens") == before


def test_a_partial_write_is_fine_once_the_profile_is_already_stamped(tmp_path, monkeypatch):
    """Control. With the unit already declared there is no stale sibling to
    re-type — the marker already describes both values truthfully."""
    from no_human.core.pricing import WEIGHTED_UNIT

    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    rp = str(repo.resolve())
    _seed_profile(db, rp, default_attempt_tokens=2_000_000,
                  default_lifetime_tokens=4_000_000,
                  default_budget_unit=WEIGHTED_UNIT)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["repo", "config", rp,
                                 "default_lifetime_tokens=6000000"])
    assert result.exit_code == 0, result.output
    prof = _get_profile(db, rp)
    assert (prof.default_attempt_tokens, prof.default_lifetime_tokens) == (
        2_000_000, 6_000_000)
    assert prof.default_budget_unit == WEIGHTED_UNIT


def test_writing_both_keys_at_once_is_the_way_out(tmp_path, monkeypatch):
    """Control, and the remedy the refusal points at: one command carrying
    both keys leaves nothing unstamped, so the marker is honest."""
    from no_human.core.pricing import WEIGHTED_UNIT

    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    rp = str(repo.resolve())
    _seed_profile(db, rp, default_attempt_tokens=10_100_000,
                  default_lifetime_tokens=20_200_000)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["repo", "config", rp,
                                 "default_attempt_tokens=2000000",
                                 "default_lifetime_tokens=8000000"])
    assert result.exit_code == 0, result.output
    prof = _get_profile(db, rp)
    assert (prof.default_attempt_tokens, prof.default_lifetime_tokens) == (
        2_000_000, 8_000_000)
    assert prof.default_budget_unit == WEIGHTED_UNIT
    assert _enforced(db, rp, "attempt_tokens") == 2_000_000, "face value now"


def test_a_partial_write_is_fine_when_the_sibling_is_unset(tmp_path, monkeypatch):
    """Control. An unset sibling is not a raw value — there is nothing to
    mis-declare, so the ordinary one-key write still works."""
    from no_human.core.pricing import WEIGHTED_UNIT

    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    rp = str(repo.resolve())
    _seed_profile(db, rp, default_lifetime_tokens=20_200_000)   # attempt = 0
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["repo", "config", rp,
                                 "default_lifetime_tokens=8000000"])
    assert result.exit_code == 0, result.output
    prof = _get_profile(db, rp)
    assert prof.default_attempt_tokens == 0
    assert prof.default_budget_unit == WEIGHTED_UNIT


def test_repo_config_inspect_reports_an_unstamped_profile_as_raw(tmp_path, monkeypatch):
    """The 12,000,000 that killed August was legible only by doing the
    arithmetic by hand. Inspect now says which unit it is in."""
    db = tmp_path / "test.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_profile(db, str(repo.resolve()), default_lifetime_tokens=12_000_000)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["repo", "config", str(repo)])
    assert result.exit_code == 0, result.output
    assert "default_lifetime_tokens=12000000" in result.output
    assert "budget_unit=raw" in result.output


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
