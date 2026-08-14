"""The human landed-override: `nh approve --landed <sha> --because ...`
and its API sibling `POST /api/tasks/{id}/approve-landed`.

The narrow class this exists for: a supervising session's squash train lands
a task's content, but automated containment (`vcs.pr_watcher`) honestly
refuses — a later train car's classification-decision edits, or a real
union-resolved source conflict, leave no candidate commit whose tree matches
the branch verbatim. `blockers/landed_override.py` is the shared, git-free
decision; this file drives it against REAL temp git repos (subprocess, in
the style of tests/test_pr_shipped.py) so ancestry and residue are genuine,
never mocked.
"""

from __future__ import annotations

import subprocess

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from no_human.api.app import app
from no_human.blockers.landed_override import (
    LANDED_OVERRIDE_KIND, OverrideRefused, approve_landed_override,
)
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus

pytestmark = pytest.mark.usefixtures("isolated_env_file")


# --------------------------------------------------------------------------- #
# git plumbing — real temp repos, no mocking of ancestry                      #
# --------------------------------------------------------------------------- #

def _git(repo_path, *args):
    subprocess.run(["git", "-C", str(repo_path), *args], check=True,
                    capture_output=True)


def _git_out(repo_path, *args):
    return subprocess.run(["git", "-C", str(repo_path), *args], text=True,
                          capture_output=True, check=True).stdout.strip()


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("orig\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def _repo_with_residue(tmp_path):
    """`sha` (main's tip) is a real ancestor of main, but `feature` still has
    a file (`b.txt`) that never landed there — containment must report it as
    residue, and the human is overriding that honest refusal."""
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("new\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "feature: add b.txt")
    _git(repo, "checkout", "main")
    (repo / "a.txt").write_text("unrelated change\n")
    _git(repo, "commit", "-am", "unrelated: change a.txt")
    return repo


@pytest_asyncio.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


async def _seed(store, repo_path, *, base_branch="main", pr_branch="feature",
                status=TaskStatus.AWAITING_APPROVAL) -> Task:
    t = Task.new("landed-override-check", repo_path=str(repo_path))
    t.context = {"base_branch": base_branch, "pr_branch": pr_branch}
    await store.create_task(t)
    if status is TaskStatus.DONE:
        await store.set_status(t, status, validate=False,
                               event={"source": "test", "kind": "test_seed"})
    elif status is not TaskStatus.PENDING:
        await store.set_status(t, status, validate=False)
    return t


# --------------------------------------------------------------------------- #
# approve_landed_override — the core, direct                                  #
# --------------------------------------------------------------------------- #

async def test_override_completes_task_and_records_audit_event(tmp_path, store):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    t = await _seed(store, repo, pr_branch="")

    result = await approve_landed_override(
        store, t, sha, "supervisor squash train 15 — verified by eyeball diff")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE

    events = await store.list_events(t.id)
    override_events = [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND]
    assert len(override_events) == 1
    ev = override_events[0]
    assert ev["sha"] == sha
    assert ev["justification"] == "supervisor squash train 15 — verified by eyeball diff"
    assert "residue" in ev
    assert "HUMAN OVERRIDE" in ev["text"]
    assert "not a containment pass" in ev["text"]
    assert "shipped" not in ev["text"].lower()
    assert result["sha"] == sha


async def test_refuses_sha_not_on_default_branch(tmp_path, store):
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "side")
    (repo / "a.txt").write_text("side change\n")
    _git(repo, "commit", "-am", "side: never merged")
    side_sha = _git_out(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    t = await _seed(store, repo)

    with pytest.raises(OverrideRefused):
        await approve_landed_override(store, t, side_sha, "asserting anyway")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert (await store.list_events(t.id)) == []


async def test_refuses_empty_justification(tmp_path, store):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    t = await _seed(store, repo)

    with pytest.raises(OverrideRefused):
        await approve_landed_override(store, t, sha, "   ")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert fresh.context.get("landed_override_sha") is None
    assert (await store.list_events(t.id)) == []


@pytest.mark.parametrize("status", [TaskStatus.IMPLEMENTING, TaskStatus.DONE])
async def test_refuses_task_not_awaiting_approval(tmp_path, store, status):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    t = await _seed(store, repo, status=status)

    with pytest.raises(OverrideRefused):
        await approve_landed_override(store, t, sha, "asserting anyway")

    fresh = await store.get_task(t.id)
    assert fresh.status is status


async def test_override_touches_no_git_state(tmp_path, store, monkeypatch):
    import no_human.vcs.approve_merge as approve_merge_mod
    import no_human.vcs.git as git_mod

    def _boom(*a, **kw):
        raise AssertionError("landed override must never touch git-mutating code")

    monkeypatch.setattr(approve_merge_mod, "land_task", _boom)
    monkeypatch.setattr(git_mod, "GitRepo", _boom)

    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    before_tip = _git_out(repo, "rev-parse", "main")
    before_status = _git_out(repo, "status", "--porcelain")
    t = await _seed(store, repo, pr_branch="")

    await approve_landed_override(store, t, sha, "no git mutation happens here")

    after_tip = _git_out(repo, "rev-parse", "main")
    after_status = _git_out(repo, "status", "--porcelain")
    assert after_tip == before_tip
    assert after_status == before_status
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE


async def test_residue_recorded_when_containment_refuses(tmp_path, store):
    repo = _repo_with_residue(tmp_path)
    sha = _git_out(repo, "rev-parse", "main")
    t = await _seed(store, repo, pr_branch="feature")

    result = await approve_landed_override(
        store, t, sha, "landed elsewhere; feature's extra file is intentional debt")

    assert result["residue"] == ["b.txt"]
    events = await store.list_events(t.id)
    ev = [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND][0]
    assert ev["residue"] == ["b.txt"]
    assert "b.txt" in ev["text"]


# --------------------------------------------------------------------------- #
# API: POST /api/tasks/{id}/approve-landed                                    #
# --------------------------------------------------------------------------- #

@pytest_asyncio.fixture
async def client(store, tmp_path):
    from no_human.config import load_config
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_api_approve_landed_endpoint(tmp_path, store, client):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    t = await _seed(store, repo, pr_branch="")

    r = await client.post(
        f"/api/tasks/{t.id}/approve-landed",
        json={"sha": sha, "justification": "supervisor squash train 15"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["sha"] == sha

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    events = await store.list_events(t.id)
    assert any(e["kind"] == LANDED_OVERRIDE_KIND for e in events)


async def test_api_approve_landed_refuses(tmp_path, store, client):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")

    # 400 on empty justification
    t1 = await _seed(store, repo, pr_branch="")
    r = await client.post(
        f"/api/tasks/{t1.id}/approve-landed",
        json={"sha": sha, "justification": "   "},
    )
    assert r.status_code == 400, r.text

    # 409 on a non-awaiting task
    t2 = await _seed(store, repo, pr_branch="", status=TaskStatus.IMPLEMENTING)
    r = await client.post(
        f"/api/tasks/{t2.id}/approve-landed",
        json={"sha": sha, "justification": "asserting anyway"},
    )
    assert r.status_code == 409, r.text

    # a repeated call (task now DONE) returns 409
    t3 = await _seed(store, repo, pr_branch="")
    r = await client.post(
        f"/api/tasks/{t3.id}/approve-landed",
        json={"sha": sha, "justification": "first call"},
    )
    assert r.status_code == 200, r.text
    r = await client.post(
        f"/api/tasks/{t3.id}/approve-landed",
        json={"sha": sha, "justification": "second call"},
    )
    assert r.status_code == 409, r.text
