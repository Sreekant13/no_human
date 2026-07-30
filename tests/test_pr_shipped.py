"""SCRUM-68 follow-up: every successfully shipped task escalated, because the
CLOSED-PR rung trusted GitHub's merged flag. The operator's hard rule is a
LOCAL, identity-normalized squash merge (never `gh pr merge`), so a squash
commit lands on the base branch with a fresh SHA that has no commit-graph
lineage back to the source branch — `git merge-base --is-ancestor` is FALSE
for every one of our merges even when the change is fully landed. These tests
pin the content-based fix: `default_branch_shipped` (git-backed) and its
wiring into WakeWatcher._check_open_pr's CLOSED rung."""

from __future__ import annotations

import subprocess

import pytest

from no_human.blockers.wake import WakeWatcher
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.vcs.pr_watcher import default_branch_shipped


def _git(repo_path, *args):
    subprocess.run(["git", "-C", str(repo_path), *args], check=True,
                    capture_output=True)


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


def _squash_merge_repo(tmp_path):
    """Branch commits a real change; main gets the SAME content via a fresh
    (squash-shaped) commit — branch head is NOT an ancestor of main, but the
    touched file's content matches."""
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("changed\n")
    _git(repo, "commit", "-am", "feature: change a.txt")
    _git(repo, "checkout", "main")
    (repo / "a.txt").write_text("changed\n")
    _git(repo, "commit", "-am", "squash: change a.txt (fresh sha, no lineage)")
    return repo


def _unshipped_repo(tmp_path):
    """Branch commits a real change that never made it to main at all."""
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "a.txt").write_text("changed\n")
    _git(repo, "commit", "-am", "feature: change a.txt")
    _git(repo, "checkout", "main")
    return repo


async def test_squash_merge_shape_is_shipped_despite_no_ancestry(tmp_path):
    repo = _squash_merge_repo(tmp_path)
    rc = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", "feature", "main"]
    ).returncode
    assert rc != 0, "fixture must NOT have branch-head ancestry into main"
    assert await default_branch_shipped(str(repo), "feature", "main") is True


async def test_genuinely_absent_branch_is_not_shipped(tmp_path):
    repo = _unshipped_repo(tmp_path)
    assert await default_branch_shipped(str(repo), "feature", "main") is False


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


async def _approval_task(store, repo_path, url="https://github.com/o/r/pull/86"):
    t = Task.new("shipped-check", repo_path=str(repo_path))
    t.context = {"pr_watch": url, "pr_branch": "feature", "base_branch": "main"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    return t


async def test_closed_pr_with_squash_merged_content_ships_not_escalates(tmp_path, store):
    repo = _squash_merge_repo(tmp_path)
    t = await _approval_task(store, repo)

    async def pr_state(url):
        return "CLOSED"

    w = WakeWatcher(store, {}, pr_state=pr_state, pr_shipped=default_branch_shipped)
    out = await w._check_open_pr(t)
    assert out == "shipped_pr_closed"
    assert (await store.get_task(t.id)).status is TaskStatus.DONE


async def test_closed_pr_with_content_genuinely_absent_still_escalates(tmp_path, store):
    repo = _unshipped_repo(tmp_path)
    t = await _approval_task(store, repo)

    async def pr_state(url):
        return "CLOSED"

    w = WakeWatcher(store, {}, pr_state=pr_state, pr_shipped=default_branch_shipped)
    out = await w._check_open_pr(t)
    assert out == "escalated_pr_closed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED
    assert "closed without merging" in fresh.blocker["question"]


async def test_closed_pr_with_no_pr_shipped_hook_falls_back_to_escalation(tmp_path, store):
    """Backward compatibility: hosts that don't wire pr_shipped keep the old
    (safe, if noisy) behavior rather than crashing."""
    repo = _unshipped_repo(tmp_path)
    t = await _approval_task(store, repo)

    async def pr_state(url):
        return "CLOSED"

    w = WakeWatcher(store, {}, pr_state=pr_state)
    out = await w._check_open_pr(t)
    assert out == "escalated_pr_closed"


async def test_a_half_landed_rename_is_not_shipped(tmp_path):
    """A branch that MOVES a file must not report shipped when only the
    destination landed on base.

    `git diff --name-only` has rename detection ON by default, so a `git mv`
    is reported as the destination path alone — the source path never enters
    the touched set and its deletion is never compared. Without
    `--no-renames` this returns True while `old.py` is still sitting on base,
    and the task is marked DONE with half its deliverable missing.
    """
    repo = _make_repo(tmp_path)
    (repo / "old.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add old")
    _git(repo, "checkout", "-b", "feature")
    _git(repo, "mv", "old.py", "new.py")
    _git(repo, "commit", "-m", "move old -> new")
    # base gets the destination but NOT the removal — a half-landed move.
    _git(repo, "checkout", "main")
    (repo / "new.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add new, forget to delete old")

    assert (repo / "old.py").exists(), "fixture: base must still carry old.py"
    assert await default_branch_shipped(str(repo), "feature", "main") is False


async def test_a_fully_landed_rename_is_shipped(tmp_path):
    """The companion: once BOTH halves of the move are on base, it ships.

    Without this, `--no-renames` could be 'fixed' by always returning False
    for any branch that renames anything.
    """
    repo = _make_repo(tmp_path)
    (repo / "old.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add old")
    _git(repo, "checkout", "-b", "feature")
    _git(repo, "mv", "old.py", "new.py")
    _git(repo, "commit", "-m", "move old -> new")
    _git(repo, "checkout", "main")
    _git(repo, "mv", "old.py", "new.py")
    _git(repo, "commit", "-m", "same move, squash-landed")

    assert await default_branch_shipped(str(repo), "feature", "main") is True


async def test_a_branch_name_that_is_also_a_path_does_not_crash(tmp_path):
    """Without a trailing `--`, git bails with 'ambiguous argument' when a
    branch name is also a path in the tree. That failed safe (escalate), but
    it meant a genuinely shipped task kept escalating forever."""
    repo = _make_repo(tmp_path)
    (repo / "dup").write_text("i am a file named like a branch\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add dup file")
    _git(repo, "checkout", "-b", "dup")
    (repo / "feat.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feature work")
    _git(repo, "checkout", "main")
    (repo / "feat.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "squash-land the same content")

    assert await default_branch_shipped(str(repo), "dup", "main") is True


async def test_a_deleted_branch_fails_safe(tmp_path):
    """The branch ref may be gone by the time the watcher looks. That must
    escalate (the old behaviour), never silently report shipped."""
    repo = _make_repo(tmp_path)
    assert await default_branch_shipped(str(repo), "no-such-branch", "main") is False


async def test_a_non_ascii_path_that_never_landed_is_not_shipped(tmp_path):
    """A branch whose touched paths need C-quoting must not report shipped.

    `core.quotePath` is on by default, so `git diff --name-only` returns
    `café.py` as the literal 7-character string `"caf\303\251.py"`. Feeding
    that back as a pathspec matches NOTHING, `git diff --quiet` then reports
    "no differences", and a branch whose entire deliverable is unmerged is
    marked DONE. `-z` emits raw NUL-separated names instead.

    No other test in this file uses a path outside [a-z0-9_.], which is why
    this survived two rounds of review.
    """
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "café.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add a non-ascii path")
    _git(repo, "checkout", "main")

    assert not (repo / "café.py").exists(), "fixture: base must NOT have the file"
    assert await default_branch_shipped(str(repo), "feature", "main") is False


async def test_a_non_ascii_path_that_did_land_is_shipped(tmp_path):
    """Companion, so the fix cannot be 'return False for anything unusual'."""
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "café.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add a non-ascii path")
    _git(repo, "checkout", "main")
    (repo / "café.py").write_text("value = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "squash-land the same content")

    assert await default_branch_shipped(str(repo), "feature", "main") is True
