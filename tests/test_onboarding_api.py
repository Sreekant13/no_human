"""Onboarding wizard API tests — real wiring, no stubs.

Exercises the /api/onboarding/* endpoints against a real Store + tmp config,
verifying: status round-trip, bounded repo detection, derive+persist of an
unproven profile, graceful IDE-absent history handling, learning-queue confirm,
and config.yaml persistence on complete.
"""
from __future__ import annotations

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
    # Environment-agnostic: whether or not a Windsurf/Devin IDE is running, the
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
