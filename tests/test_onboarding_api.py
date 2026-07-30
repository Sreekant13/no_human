"""Onboarding wizard API tests — real wiring, no stubs.

Exercises the /api/onboarding/* endpoints against a real Store + tmp config,
verifying: status round-trip, bounded repo detection, derive+persist of an
unproven profile, graceful IDE-absent history handling, learning-queue confirm,
and config.yaml persistence on complete.
"""
from __future__ import annotations

import os
import types

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import no_human.config as nh_config
from no_human.api.app import app
from no_human.core.db import Store
from no_human.learning import LearningQueue, TYPE_RULE


@pytest_asyncio.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "test.db").connect()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def client(store, tmp_path, monkeypatch):
    app.state.store = store
    # Lightweight config stand-in; persist target redirected off the real ~/.no_human.
    app.state.config = types.SimpleNamespace(
        data={"git": {"github_hosts": ["github.com", "code.example.com"]}}
    )
    monkeypatch.setattr(nh_config, "CONFIG_PATH", tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_status_initially_incomplete(client):
    r = await client.get("/api/onboarding/status")
    assert r.status_code == 200
    assert r.json()["completed"] is False


@pytest.mark.asyncio
async def test_detect_repos_bounded(client, tmp_path):
    # Two repos at different depths + a non-repo dir.
    (tmp_path / "proj-a" / ".git").mkdir(parents=True)
    (tmp_path / "proj-a" / "package.json").write_text('{"scripts":{"test":"x"}}')
    (tmp_path / "nested" / "proj-b" / ".git").mkdir(parents=True)
    (tmp_path / "nested" / "proj-b" / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / "not-a-repo").mkdir()

    r = await client.post("/api/onboarding/repos/detect", json={"root": str(tmp_path)})
    assert r.status_code == 200
    repos = r.json()["repos"]
    names = {x["name"]: x["ecosystem"] for x in repos}
    assert "proj-a" in names and names["proj-a"] == "node"
    assert "proj-b" in names and names["proj-b"] == "python"
    assert "not-a-repo" not in names


@pytest.mark.asyncio
async def test_detect_missing_root(client, tmp_path):
    r = await client.post("/api/onboarding/repos/detect", json={"root": str(tmp_path / "nope")})
    assert r.status_code == 200
    assert r.json()["repos"] == []
    assert "not a directory" in r.json()["error"]


@pytest.mark.asyncio
async def test_onboard_repo_persists_unproven_profile(client, store, tmp_path):
    repo = tmp_path / "svc"
    (repo / ".git").mkdir(parents=True)
    repo.joinpath("package.json").write_text('{"scripts":{"test":"jest","lint":"eslint"}}')
    repo.joinpath("package-lock.json").write_text("{}")

    r = await client.post("/api/onboarding/repos/onboard", json={"repo_path": str(repo)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ecosystem"] == "node"
    assert body["install_cmd"] == "npm ci"
    assert body["proven"] is False
    # Persisted and retrievable, unconfirmed + unproven.
    prof = await store.get_profile(str(repo.resolve()))
    assert prof is not None
    assert prof.confirmed is False
    assert prof.is_usable is False  # not proven → never drives a task


@pytest.mark.asyncio
async def test_onboard_rejects_non_repo(client, tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    r = await client.post("/api/onboarding/repos/onboard", json={"repo_path": str(d)})
    assert r.status_code == 422


@pytest.mark.asyncio
@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_history_extract_returns_valid_shape(client):
    # Environment-agnostic: whether or not a Windsurf IDE is running, the  # term-ok: real IDE names
    # endpoint must return 200 with a well-formed, honest shape (never crash).
    r = await client.post("/api/onboarding/history/extract")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["available"], bool)
    assert isinstance(body["transcripts"], int) and body["transcripts"] >= 0


@pytest.mark.asyncio
async def test_history_analyze_returns_valid_shape(client):
    r = await client.post("/api/onboarding/history/analyze", json={"days": 7})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["proposed"], int) and body["proposed"] >= 0
    # Anything proposed must be queue-visible (source="proposed").
    if body["proposed"]:
        q = LearningQueue(app.state.store)
        pending_ids = {m["id"] for m in await q.pending()}
        assert all(p["id"] in pending_ids for p in body["proposals"])


@pytest.mark.asyncio
async def test_confirm_rules_activates_proposal(client, store):
    # Seed an unconfirmed proposal, then confirm it via the onboarding endpoint.
    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="Verify everything",
        content="No assumptions — run it and cite evidence.",
        source="proposed", confirmed=False,
    )
    assert mem_id
    q = LearningQueue(store)
    assert any(m["id"] == mem_id for m in await q.pending())

    r = await client.post("/api/onboarding/rules/confirm", json={"ids": [mem_id]})
    assert r.status_code == 200
    assert r.json()["confirmed"] == 1
    # Now active, no longer pending.
    assert any(m["id"] == mem_id for m in await q.active())
    assert not any(m["id"] == mem_id for m in await q.pending())


@pytest.mark.asyncio
async def test_complete_persists_and_status_reflects(client, tmp_path):
    payload = {"team": "METRICSDB", "repos": ["/x/svc"], "docs": ["/docs/adr"]}
    r = await client.post("/api/onboarding/complete", json=payload)
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Status now complete, and config.yaml was written to the redirected path.
    s = await client.get("/api/onboarding/status")
    body = s.json()
    assert body["completed"] is True
    assert body["team"] == "METRICSDB"
    assert (tmp_path / "config.yaml").exists()


# --------------------------------------------------------------------------- #
# GET /api/repos/discover — the typed-path replacement. The endpoint is bound  #
# to the process's home, so these tests relocate HOME onto tmp_path rather     #
# than accepting a caller-supplied root (which would be an unbounded scan      #
# surface on a localhost service).                                             #
# --------------------------------------------------------------------------- #

def _seed_repo(path):
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return path


@pytest.mark.asyncio
async def test_discover_lists_repos_from_the_conventional_roots(client, tmp_path, monkeypatch):
    home = tmp_path / "home"
    _seed_repo(home / "git" / "svc-a")
    _seed_repo(home / "Projects" / "svc-b")
    monkeypatch.setenv("HOME", str(home))

    r = await client.get("/api/repos/discover")
    assert r.status_code == 200, r.text
    body = r.json()
    names = {x["name"] for x in body["repos"]}
    assert names == {"svc-a", "svc-b"}
    row = next(x for x in body["repos"] if x["name"] == "svc-a")
    assert row["path"] == str(home / "git" / "svc-a")
    assert row["is_git"] is True
    assert row["branch"] == "main"
    assert row["dirty"] is False
    assert body["capped"] is False
    assert isinstance(body["elapsed_ms"], int)


@pytest.mark.asyncio
async def test_discover_honours_operator_configured_extra_roots(client, tmp_path, monkeypatch):
    home = tmp_path / "home"
    _seed_repo(home / "work" / "acme")
    monkeypatch.setenv("HOME", str(home))
    app.state.config.data["onboarding"] = {"extra_scan_roots": ["~/work"]}
    try:
        r = await client.get("/api/repos/discover")
        assert {x["name"] for x in r.json()["repos"]} == {"acme"}
    finally:
        app.state.config.data.pop("onboarding", None)


@pytest.mark.asyncio
async def test_discover_caps_and_says_so_rather_than_truncating_silently(client, tmp_path, monkeypatch):
    home = tmp_path / "home"
    for i in range(6):
        _seed_repo(home / "git" / f"r{i}")
    monkeypatch.setenv("HOME", str(home))

    r = await client.get("/api/repos/discover", params={"limit": 2})
    body = r.json()
    assert len(body["repos"]) == 2
    assert body["capped"] is True
    assert body["total_found"] == 6
    assert body["note"]


@pytest.mark.asyncio
async def test_discover_clamps_an_absurd_limit(client, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    r = await client.get("/api/repos/discover", params={"limit": 10 ** 6})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_discover_on_a_bare_home_is_an_empty_list_not_an_error(client, tmp_path, monkeypatch):
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    r = await client.get("/api/repos/discover")
    assert r.status_code == 200
    assert r.json()["repos"] == []
    assert r.json()["roots_scanned"] == []


@pytest.mark.asyncio
async def test_discover_survives_an_unreadable_directory_instead_of_500ing(
    client, tmp_path, monkeypatch
):
    """One ``chmod 000`` folder anywhere under a scan root used to raise
    ``PermissionError`` straight out of the walk. This endpoint has no
    exception handler in front of it, so that was an unhandled 500 on the
    FIRST-RUN onboarding path, and it took the healthy repos down with it.
    """
    home = tmp_path / "home"
    _seed_repo(home / "Projects" / "healthy")
    locked = home / "Projects" / "locked"
    (locked / ".git").mkdir(parents=True)
    locked.chmod(0o000)
    monkeypatch.setenv("HOME", str(home))
    try:
        # Prove the scenario is genuinely hostile before trusting the result.
        # (Under root the bits mean nothing; the assertions below still hold,
        # they just prove less there. A skip marker would cost a tamper signal
        # for no gain.)
        if not (hasattr(os, "geteuid") and os.geteuid() == 0):
            with pytest.raises(PermissionError):
                (locked / ".git").exists()
        r = await client.get("/api/repos/discover")
    finally:
        locked.chmod(0o755)

    assert r.status_code == 200, r.text
    assert {x["name"] for x in r.json()["repos"]} == {"healthy"}


@pytest.mark.asyncio
async def test_discover_refuses_a_conventional_root_that_leaves_home(
    client, tmp_path, monkeypatch
):
    """``~/Dev -> /Volumes/BigDisk`` is an ordinary setup, and it walked the
    scan straight out of the home directory this endpoint is bound to."""
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    _seed_repo(outside / "secretrepo")
    (home / "Dev").symlink_to(outside)
    monkeypatch.setenv("HOME", str(home))

    r = await client.get("/api/repos/discover")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["repos"] == []
    assert body["roots_refused"], "a refused root must be reported, not silently dropped"
