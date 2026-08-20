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
import time
from datetime import datetime

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


def _make_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("orig\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def _repo_with_residue(tmp_path, name="repo"):
    """`sha` (main's tip) is a real ancestor of main, but `feature` still has
    a file (`b.txt`) that never landed there — containment must report it as
    residue, and the human is overriding that honest refusal."""
    repo = _make_repo(tmp_path, name)
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("new\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "feature: add b.txt")
    _git(repo, "checkout", "main")
    (repo / "a.txt").write_text("unrelated change\n")
    _git(repo, "commit", "-am", "unrelated: change a.txt")
    return repo


def _repo_with_squash_landed(tmp_path, name="repo"):
    """The `failed_pre_pr` shape's happy path: `feature`'s content is
    hand-landed onto `main` as a SEPARATE commit (not a merge, not a
    fast-forward — the same shape a supervising session's squash train, or a
    human `git cherry-pick`, produces) whose tree is content-equivalent to
    `feature`. Returns ``(repo, feature_sha, landed_sha)``."""
    repo = _make_repo(tmp_path, name)
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("new\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "feature: add b.txt")
    feature_sha = _git_out(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    (repo / "b.txt").write_text("new\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "hand-landed: add b.txt")
    landed_sha = _git_out(repo, "rev-parse", "main")
    return repo, feature_sha, landed_sha


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


async def _seed_failed_pre_pr(
    store, repo_path, *, branch="feature", commit_sha="",
    base_branch="main", pr_evidence=None, cancel_reason=None,
) -> Task:
    """The 5b2246c1 shape: FAILED, pre-PR (no `pr_branch` ever written to
    context — content is only locatable via attempt metadata), a
    BUDGET_EXHAUSTED blocker, and no PR event, unless `pr_evidence` is
    given."""
    t = Task.new("landed-override-check", repo_path=str(repo_path))
    t.context = {"base_branch": base_branch}
    if pr_evidence:
        t.context["pr_watch"] = pr_evidence
    if cancel_reason:
        t.context["cancel_reason"] = cancel_reason
    t.blocker = {"category": "BUDGET_EXHAUSTED",
                 "message": "8.29M/8M lifetime token cap"}
    await store.create_task(t)
    attempt_id = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        attempt_id, branch_name=branch, commit_sha=commit_sha,
        status="failed", failure_reason="BUDGET_EXHAUSTED")
    await store.set_status(t, TaskStatus.FAILED, validate=False)
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


@pytest.mark.parametrize(
    "status", [TaskStatus.IMPLEMENTING, TaskStatus.DONE, TaskStatus.ESCALATED])
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

    # same poisoned monkeypatches, the failed-pre-PR shape
    repo2, feature_sha, landed_sha = _repo_with_squash_landed(tmp_path, name="repo2")
    before_tip2 = _git_out(repo2, "rev-parse", "main")
    before_status2 = _git_out(repo2, "status", "--porcelain")
    t2 = await _seed_failed_pre_pr(
        store, repo2, branch="feature", commit_sha=feature_sha)

    await approve_landed_override(
        store, t2, landed_sha, "no git mutation happens here either")

    after_tip2 = _git_out(repo2, "rev-parse", "main")
    after_status2 = _git_out(repo2, "status", "--porcelain")
    assert after_tip2 == before_tip2
    assert after_status2 == before_status2
    fresh2 = await store.get_task(t2.id)
    assert fresh2.status is TaskStatus.DONE


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
# the "failed_pre_pr" shape — budget-failed-no-PR, live incident 5b2246c1     #
# --------------------------------------------------------------------------- #

async def test_failed_pre_pr_task_with_landed_content_completes(tmp_path, store):
    repo, feature_sha, landed_sha = _repo_with_squash_landed(tmp_path)
    t = await _seed_failed_pre_pr(
        store, repo, branch="feature", commit_sha=feature_sha)

    result = await approve_landed_override(
        store, t, landed_sha, "hand-landed by operator")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE

    events = await store.list_events(t.id)
    override_events = [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND]
    assert len(override_events) == 1
    ev = override_events[0]
    assert ev["sha"] == landed_sha
    assert ev["justification"] == "hand-landed by operator"
    assert ev["shape"] == "failed_pre_pr"
    assert ev["prior_status"] == "failed"
    assert "HUMAN OVERRIDE" in ev["text"]

    assert fresh.context.get("landed_override_sha") == landed_sha
    assert "landed_sha" not in fresh.context
    assert result["sha"] == landed_sha
    assert result["shape"] == "failed_pre_pr"
    assert result["prior_status"] == "failed"


async def test_failed_pre_pr_records_content_equivalence(tmp_path, store):
    repo, feature_sha, landed_sha = _repo_with_squash_landed(tmp_path)
    t = await _seed_failed_pre_pr(
        store, repo, branch="feature", commit_sha=feature_sha)

    result = await approve_landed_override(
        store, t, landed_sha, "content matches byte for byte")

    events = await store.list_events(t.id)
    ev = [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND][0]
    assert ev["equivalence"] == "content_equivalent"
    assert ev["residue"] == []
    assert result["residue"] == []


async def test_failed_pre_pr_with_residue_completes_on_assertion(tmp_path, store):
    repo = _repo_with_residue(tmp_path)
    main_sha = _git_out(repo, "rev-parse", "main")
    t = await _seed_failed_pre_pr(store, repo, branch="feature", commit_sha="")

    result = await approve_landed_override(
        store, t, main_sha,
        "landed elsewhere; feature's extra file is intentional debt")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    events = await store.list_events(t.id)
    ev = [e for e in events if e["kind"] == LANDED_OVERRIDE_KIND][0]
    assert ev["equivalence"] == "asserted_with_residue"
    assert ev["residue"] == ["b.txt"]
    assert result["residue"] == ["b.txt"]


async def test_failed_pre_pr_refuses_sha_not_on_base(tmp_path, store):
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "side")
    (repo / "a.txt").write_text("side change\n")
    _git(repo, "commit", "-am", "side: never merged")
    side_sha = _git_out(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    t = await _seed_failed_pre_pr(store, repo, branch="side", commit_sha=side_sha)

    with pytest.raises(OverrideRefused):
        await approve_landed_override(store, t, side_sha, "asserting anyway")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.FAILED
    assert (await store.list_events(t.id)) == []


async def test_failed_task_with_pr_evidence_is_refused(tmp_path, store):
    repo, feature_sha, landed_sha = _repo_with_squash_landed(tmp_path)
    t = await _seed_failed_pre_pr(
        store, repo, branch="feature", commit_sha=feature_sha,
        pr_evidence="https://example.com/pull/1")

    with pytest.raises(OverrideRefused, match="restore-approval"):
        await approve_landed_override(store, t, landed_sha, "asserting anyway")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.FAILED
    assert (await store.list_events(t.id)) == []


async def test_failed_cancelled_task_is_refused(tmp_path, store):
    repo, feature_sha, landed_sha = _repo_with_squash_landed(tmp_path)
    t = await _seed_failed_pre_pr(
        store, repo, branch="feature", commit_sha=feature_sha,
        cancel_reason="operator")

    with pytest.raises(OverrideRefused, match="cancelled"):
        await approve_landed_override(store, t, landed_sha, "asserting anyway")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.FAILED
    assert (await store.list_events(t.id)) == []


async def test_failed_pre_pr_requires_justification(tmp_path, store):
    repo, feature_sha, landed_sha = _repo_with_squash_landed(tmp_path)
    t = await _seed_failed_pre_pr(
        store, repo, branch="feature", commit_sha=feature_sha)

    with pytest.raises(OverrideRefused):
        await approve_landed_override(store, t, landed_sha, "   ")

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.FAILED
    assert fresh.context.get("landed_override_sha") is None
    assert (await store.list_events(t.id)) == []


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


async def test_api_approve_landed_completes_failed_pre_pr(tmp_path, store, client):
    repo, feature_sha, landed_sha = _repo_with_squash_landed(tmp_path)
    t = await _seed_failed_pre_pr(
        store, repo, branch="feature", commit_sha=feature_sha)

    r = await client.post(
        f"/api/tasks/{t.id}/approve-landed",
        json={"sha": landed_sha, "justification": "hand-landed by operator"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["sha"] == landed_sha

    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.DONE
    events = await store.list_events(t.id)
    assert any(e["kind"] == LANDED_OVERRIDE_KIND for e in events)

    # replay cannot append a duplicate override event
    r = await client.post(
        f"/api/tasks/{t.id}/approve-landed",
        json={"sha": landed_sha, "justification": "replay"},
    )
    assert r.status_code == 409, r.text


# --------------------------------------------------------------------------- #
# ts must be the shared REAL clock, not an ISO string (SQLite orders TEXT     #
# above REAL, so an ISO ts sorts above every real event forever)              #
# --------------------------------------------------------------------------- #

async def test_override_event_ts_is_real_and_orders_before_later_event(
    tmp_path, store,
):
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "HEAD")
    t = await _seed(store, repo, pr_branch="")

    result = await approve_landed_override(
        store, t, sha, "supervisor squash train 15 — verified by eyeball diff")

    rows = await store.query(
        "SELECT typeof(ts) AS t, ts FROM task_events "
        "WHERE json_extract(data,'$.kind')=?",
        (LANDED_OVERRIDE_KIND,),
    )
    assert len(rows) == 1
    assert rows[0]["t"] == "real"

    # a normal event emitted afterwards must sort AFTER the override, not
    # before it — the bug made every override look newer than everything.
    await store.save_events(
        t.id, [{"kind": "note", "source": "test", "ts": time.time()}])

    ordered = await store.query(
        "SELECT json_extract(data,'$.kind') AS k FROM task_events "
        "ORDER BY ts ASC, id ASC",
    )
    kinds = [r["k"] for r in ordered]
    assert kinds.index(LANDED_OVERRIDE_KIND) < kinds.index("note")

    fresh = await store.get_task(t.id)
    approved_at = fresh.context["approved_at"]
    # still ISO, still parseable — the drawer and web/e2e/drawer.mjs depend
    # on this shape; only the event-level ts column changed type.
    datetime.fromisoformat(approved_at)
    assert result["sha"] == sha
