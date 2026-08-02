"""ProjectProfile: YAML round-trip, readiness gate, and SQLite mirror."""

import pytest

from no_human.core.db import Store
from no_human.profile import PROFILE_RELPATH, ProjectProfile


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "t.db").connect()
    yield s
    await s.close()


def _sample(repo_path):
    return ProjectProfile(
        repo_path=str(repo_path),
        ecosystem="python-pytest",
        install_cmd="uv sync",
        test_cmd="uv run pytest -q",
        lint_cmd="uv run ruff check",
        ci={"backend": "gitlab", "project": "ci_gate/subgroup/metrics-core"},
        human_gated_steps=["build image on Jenkins"],
        derived_from=["pyproject.toml", ".gitlab-ci.yml"],
        proven={"test_cmd": True, "install_cmd": True},
        confirmed=True,
        notes="derived by nh onboard",
    )


def test_yaml_round_trip(tmp_path):
    prof = _sample(tmp_path)
    path = prof.save()
    assert path == tmp_path / PROFILE_RELPATH
    assert path.exists()
    # repo_path is implied by location, not written into the file.
    assert "repo_path" not in path.read_text()

    loaded = ProjectProfile.load(tmp_path)
    assert loaded is not None
    assert loaded.repo_path == str(tmp_path)
    assert loaded.test_cmd == "uv run pytest -q"
    assert loaded.ci == {"backend": "gitlab", "project": "ci_gate/subgroup/metrics-core"}
    assert loaded.human_gated_steps == ["build image on Jenkins"]
    assert loaded.proven == {"test_cmd": True, "install_cmd": True}
    assert loaded.confirmed is True


def test_load_missing_returns_none(tmp_path):
    assert ProjectProfile.load(tmp_path) is None


def test_from_dict_ignores_unknown_keys():
    prof = ProjectProfile.from_dict(
        {"repo_path": "/r", "test_cmd": "pytest", "bogus": 1, "legacy_field": "x"}
    )
    assert prof.repo_path == "/r"
    assert prof.test_cmd == "pytest"


def test_is_usable_gate():
    # confirmed + test_cmd present + test_cmd proven -> usable
    assert _sample("/r").is_usable is True
    # not confirmed -> not usable even if proven
    p = _sample("/r")
    p.confirmed = False
    assert p.is_usable is False
    # confirmed but test never proven -> not usable (trust requires proof)
    p2 = _sample("/r")
    p2.proven = {}
    assert p2.is_usable is False
    # confirmed, no test command -> not usable
    p3 = _sample("/r")
    p3.test_cmd = ""
    assert p3.is_usable is False


def test_usable_under_policy():
    # confirmed profile is usable regardless of the flag
    assert _sample("/r").usable_under_policy(auto_confirm_proven=False) is True
    # proven-but-unconfirmed: usable ONLY when the flag is on (the ca23ce68 fix)
    p = _sample("/r")
    p.confirmed = False
    assert p.usable_under_policy(auto_confirm_proven=False) is False
    assert p.usable_under_policy(auto_confirm_proven=True) is True
    # the flag never bypasses PROOF — an unproven profile stays unusable
    p2 = _sample("/r")
    p2.confirmed = False
    p2.proven = {}
    assert p2.usable_under_policy(auto_confirm_proven=True) is False


async def test_store_upsert_and_get(store, tmp_path):
    prof = _sample(tmp_path)
    await store.upsert_profile(prof)
    got = await store.get_profile(str(tmp_path))
    assert got is not None
    assert got.ecosystem == "python-pytest"
    assert got.ci["project"] == "ci_gate/subgroup/metrics-core"
    assert got.confirmed is True
    assert got.proven["test_cmd"] is True


async def test_store_get_missing_returns_none(store):
    assert await store.get_profile("/nope") is None


async def test_store_upsert_updates_existing(store, tmp_path):
    prof = _sample(tmp_path)
    prof.confirmed = False
    await store.upsert_profile(prof)
    prof.confirmed = True
    prof.test_cmd = "uv run pytest -q tests/"
    await store.upsert_profile(prof)              # second upsert = update, not dup
    got = await store.get_profile(str(tmp_path))
    assert got.confirmed is True
    assert got.test_cmd == "uv run pytest -q tests/"
    # exactly one row for this repo
    cur = await store.db.execute(
        "SELECT COUNT(*) c FROM project_profiles WHERE repo_path = ?", (str(tmp_path),)
    )
    assert (await cur.fetchone())["c"] == 1