"""Orchestrator consumes a confirmed ProjectProfile: test_cmd resolution
precedence + usability gating (replaces the detect_command heuristic)."""

from types import SimpleNamespace

import pytest

from no_human.config import load_config
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.notify.slack import SlackNotifier
from no_human.profile import ProjectProfile


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover - not exercised here
        raise AssertionError("backend should not run in resolution tests")


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


def _orch(store, tmp_path, *, tests_command=None):
    cfg = load_config(tmp_path / "config.yaml")
    if tests_command is not None:
        cfg.data.setdefault("tests", {})["command"] = tests_command
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))


def _repo(path):
    return SimpleNamespace(path=path)


def _usable(repo_path):
    return ProjectProfile(
        repo_path=str(repo_path), ecosystem="node",
        install_cmd="npm ci", test_cmd="npm test",
        ci={"backend": "gitlab", "enabled": True, "project": "x/y"},
        derived_from=["package.json"], proven={"test_cmd": True}, confirmed=True,
    )


async def test_usable_profile_test_cmd_used(store, tmp_path):
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    await store.upsert_profile(_usable(repo_path))
    orch = _orch(store, tmp_path)
    assert await orch._resolve_test_cmd(_repo(repo_path)) == "npm test"


async def test_explicit_config_command_wins_over_profile(store, tmp_path):
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    await store.upsert_profile(_usable(repo_path))
    orch = _orch(store, tmp_path, tests_command="pytest -q --override")
    assert await orch._resolve_test_cmd(_repo(repo_path)) == "pytest -q --override"


async def test_unconfirmed_profile_is_not_usable(store, tmp_path):
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    prof = _usable(repo_path); prof.confirmed = False
    await store.upsert_profile(prof)
    orch = _orch(store, tmp_path)
    # not usable → None → run_tests falls back to detect_command
    assert await orch._usable_profile(repo_path) is None
    assert await orch._resolve_test_cmd(_repo(repo_path)) is None


async def test_proven_but_unconfirmed_or_unproven_blocked(store, tmp_path):
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    # confirmed but test_cmd NOT proven → still not usable (trust requires proof).
    prof = _usable(repo_path); prof.proven = {}
    await store.upsert_profile(prof)
    orch = _orch(store, tmp_path)
    assert await orch._usable_profile(repo_path) is None


async def test_auto_confirm_proven_makes_proven_unconfirmed_usable(store, tmp_path):
    # P1: a PROVEN but human-unconfirmed profile becomes usable when the
    # auto_confirm_proven policy is opted in.
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    prof = _usable(repo_path); prof.confirmed = False
    await store.upsert_profile(prof)
    orch = _orch(store, tmp_path)
    assert await orch._usable_profile(repo_path) is None      # default: off
    orch.config.setdefault("profile", {})["auto_confirm_proven"] = True
    got = await orch._usable_profile(repo_path)
    assert got is not None and got.test_cmd == "npm test"
    assert await orch._resolve_test_cmd(_repo(repo_path)) == "npm test"


async def test_auto_confirm_proven_still_requires_proof(store, tmp_path):
    # P1: the policy removes the human click, NEVER the proof requirement.
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    prof = _usable(repo_path); prof.confirmed = False; prof.proven = {}
    await store.upsert_profile(prof)
    orch = _orch(store, tmp_path)
    orch.config.setdefault("profile", {})["auto_confirm_proven"] = True
    assert await orch._usable_profile(repo_path) is None


async def test_no_profile_falls_back_to_none(store, tmp_path):
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    orch = _orch(store, tmp_path)
    assert await orch._resolve_test_cmd(_repo(repo_path)) is None


async def test_profile_loaded_from_repo_yaml_when_not_in_store(store, tmp_path):
    repo_path = tmp_path / "repo"; repo_path.mkdir()
    _usable(repo_path).save()  # writes .no_human/project.yml only
    orch = _orch(store, tmp_path)
    prof = await orch._usable_profile(repo_path)
    assert prof is not None and prof.test_cmd == "npm test"
