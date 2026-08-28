"""`nh approve --ready [--yes]` — list, then land, every AWAITING_APPROVAL
task whose merge_policy verdict is ready for its CURRENT branch head, and
`nh status`'s `merge-ready: N` line.

Two different algorithms are under test here, not one:

* `--ready`'s LISTING/landing path (`_go_ready` in cli/commands.py) does a
  LIVE git resolution — fetch, resolve the PR branch, `rev-parse` its tip —
  and looks the verdict up under that live head sha. A verdict stamped for
  an older commit, or one whose diff touched the policy file itself
  (`policy_changed_in_diff`), does not count as ready.
* `nh status`'s `merge-ready: N` count (`api/models.py:merge_ready_for`) is
  DB-only: it reads the LATEST attempt's `commit_sha` column and looks the
  verdict up under that — no git at all.

Idiom: real temp git repos (subprocess), `_bootstrap` patched the way
`tests/test_approve.py` does; `land_task` is always faked via
`monkeypatch.setattr(approve_merge_mod, "land_task", ...)` (the local import
inside `_land_one` re-resolves the module attribute on every call), so no
`origin`/`gh` stub is needed — `GitRepo.fetch()` no-ops on a repo with no
configured remote and `GitRepo.resolve_commitish()` resolves a purely local
branch directly.
"""

from __future__ import annotations

import asyncio
import subprocess
import unittest.mock as mock

from click.testing import CliRunner

import no_human.vcs.approve_merge as approve_merge_mod
from no_human.cli.commands import approve, status
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.vcs.approve_merge import LandResult

# --------------------------------------------------------------------------- #
# git plumbing — real temp repos (copied from tests/test_approve.py)          #
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


def _repo_with_feature_branch(tmp_path, name="repo"):
    """A `main` with one commit, plus a `feature` branch one commit ahead —
    purely local, never pushed anywhere. Returns (repo_path, head_sha) where
    head_sha is `feature`'s tip — exactly what `resolve_commitish("feature")`
    + `rev-parse` resolve to inside `_go_ready`/`_land_one`."""
    repo = _make_repo(tmp_path, name)
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("change\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "feature commit")
    _git(repo, "checkout", "main")
    head_sha = _git_out(repo, "rev-parse", "feature")
    return repo, head_sha


# --------------------------------------------------------------------------- #
# CLI harness (copied from tests/test_approve.py)                             #
# --------------------------------------------------------------------------- #

class _Cfg:
    db_path = None
    data: dict = {}

    def get(self, key, default=None):
        return self.data.get(key, default)


def _cfg(db_path):
    c = _Cfg()
    c.db_path = db_path
    return c


def _invoke(cmd, db, args):
    import no_human.cli.commands as cmd_mod
    with mock.patch.object(cmd_mod, "_bootstrap",
                           lambda require_auth=False: (_cfg(db), None)), \
         mock.patch.object(cmd_mod, "_probe_pool",
                           lambda _cfg: cmd_mod.PoolProbe(None, cmd_mod.POOL_REFUSED)):
        return CliRunner().invoke(cmd, args)


def _task_state(db, task_id):
    async def _go():
        async with Store(db) as store:
            t = await store.get_task(task_id)
            events = await store.list_events(task_id)
            return t, events
    return asyncio.run(_go())


def _awaiting_order(db):
    """The actual `store.list_tasks()` order (`ORDER BY created_at DESC`) —
    "discovery order" as `--ready` sees it must be read from the DB, never
    assumed from creation sequence (ties at timestamp resolution)."""
    async def _go():
        async with Store(db) as store:
            tasks = await store.list_tasks()
            return [t.id for t in tasks if t.status == TaskStatus.AWAITING_APPROVAL]
    return asyncio.run(_go())


_RULES = [
    {"name": "tests_pass", "passed": True, "detail": "12/12 passed"},
    {"name": "no_todo", "passed": True, "detail": ""},
]


def _ready_task(db, tmp_path, *, title, repo_name, review_passed=True,
                 mp_ready=True, mp_policy_changed=False, mp_sha=None):
    """An AWAITING_APPROVAL task with a real local feature branch, a
    `pr_watch`/`pr_branch` pair (all `resolve_task_pr` needs — no PR event
    log required), a `merge_policy` verdict, and a `review_history` round
    stamped on the branch head (all `_review_pass_evidence` needs). Returns
    (task_id, repo_path, head_sha)."""
    repo, head_sha = _repo_with_feature_branch(tmp_path, repo_name)
    verdict_sha = mp_sha if mp_sha is not None else head_sha

    async def _go():
        async with Store(db) as store:
            t = Task.new(title, repo_path=str(repo))
            t.context = {
                "pr_watch": "https://example.invalid/pr/1",
                "pr_branch": "feature",
                "merge_policy": {
                    verdict_sha: {
                        "ready": mp_ready,
                        "policy_changed_in_diff": mp_policy_changed,
                        "rules": [dict(r) for r in _RULES],
                    },
                },
                "review_history": [{"sha": head_sha, "passed": review_passed}],
            }
            await store.create_task(t)
            await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
            return t.id
    return asyncio.run(_go()), repo, head_sha


def _never_called_land_task(*args, **kwargs):
    raise AssertionError("land_task must not be called")


# --------------------------------------------------------------------------- #
# --ready (list only)                                                         #
# --------------------------------------------------------------------------- #

def test_ready_without_yes_lists_and_lands_nothing(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    id_a, _, _ = _ready_task(db, tmp_path, title="Task A", repo_name="repo-a")
    id_b, _, _ = _ready_task(db, tmp_path, title="Task B", repo_name="repo-b")

    result = _invoke(approve, db, ["--ready"])

    assert result.exit_code == 0, result.output
    assert id_a[:8] in result.output
    assert id_b[:8] in result.output
    assert "Task A" in result.output
    assert "Task B" in result.output
    assert result.output.count("rules 2/2") == 2
    assert "https://example.invalid/pr/1" in result.output
    assert "2 task(s) merge-ready" in result.output
    assert "--yes to land them" in result.output

    t_a, _ = _task_state(db, id_a)
    t_b, _ = _task_state(db, id_b)
    assert t_a.status is TaskStatus.AWAITING_APPROVAL
    assert t_b.status is TaskStatus.AWAITING_APPROVAL


# --------------------------------------------------------------------------- #
# --ready --yes                                                               #
# --------------------------------------------------------------------------- #

def test_ready_yes_lands_in_discovery_order(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    id_a, _, _ = _ready_task(db, tmp_path, title="Task A", repo_name="repo-a")
    id_b, _, _ = _ready_task(db, tmp_path, title="Task B", repo_name="repo-b")

    expected_order = _awaiting_order(db)
    assert set(expected_order) == {id_a, id_b}

    seen_order = []

    def _fake(*, task_id, **kwargs):
        seen_order.append(task_id)
        return LandResult(ok=True, step="close_pr",
                           landed_sha="ab" * 20, message="landed by fake")

    monkeypatch.setattr(approve_merge_mod, "land_task", _fake)

    result = _invoke(approve, db, ["--ready", "--yes"])

    assert result.exit_code == 0, result.output
    assert seen_order == expected_order
    assert result.output.count("merged") == 2
    assert ("ab" * 20)[:12] in result.output  # landed_sha[:12], as printed

    t_a, _ = _task_state(db, id_a)
    t_b, _ = _task_state(db, id_b)
    assert t_a.status is TaskStatus.DONE
    assert t_b.status is TaskStatus.DONE


def test_ready_yes_stops_at_first_failure(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    id_a, _, _ = _ready_task(db, tmp_path, title="Task A", repo_name="repo-a")
    id_b, _, _ = _ready_task(db, tmp_path, title="Task B", repo_name="repo-b")

    expected_order = _awaiting_order(db)
    first_id, second_id = expected_order

    calls = []

    def _fake(*, task_id, **kwargs):
        calls.append(task_id)
        if len(calls) > 1:
            raise AssertionError("land_task must not be called for the second task")
        return LandResult(ok=False, step="push", stderr="")

    monkeypatch.setattr(approve_merge_mod, "land_task", _fake)

    result = _invoke(approve, db, ["--ready", "--yes"])

    assert result.exit_code == 1, result.output
    assert calls == [first_id]
    assert "step 'push'" in result.output
    assert "landed 0/2 before stopping." in result.output

    t_first, _ = _task_state(db, first_id)
    t_second, _ = _task_state(db, second_id)
    assert t_first.status is TaskStatus.AWAITING_APPROVAL
    assert t_second.status is TaskStatus.AWAITING_APPROVAL


def test_ready_yes_continues_past_skipped_land(tmp_path, monkeypatch):
    """`land_task` returns `skipped=True, ok=True` on entirely normal,
    non-failure paths — `gh` not installed, or `approve_merge` disabled
    (vcs/approve_merge.py:620-629). Single-task `nh approve <task_id>`
    treats that as success ("approved ... merge the PR in your git host",
    exit 0, no sys.exit). The batch must match: a skipped land is not a
    "stop at the first failure" — it must count as a completed step and the
    batch must keep walking the remaining ready tasks."""
    db = tmp_path / "nh.db"
    id_a, _, _ = _ready_task(db, tmp_path, title="Task A", repo_name="repo-a")
    id_b, _, _ = _ready_task(db, tmp_path, title="Task B", repo_name="repo-b")

    expected_order = _awaiting_order(db)
    first_id, second_id = expected_order

    calls = []

    def _fake(*, task_id, **kwargs):
        calls.append(task_id)
        return LandResult(ok=True, step="preconditions", skipped=True,
                           message="gh CLI not found — cannot merge automatically")

    monkeypatch.setattr(approve_merge_mod, "land_task", _fake)

    result = _invoke(approve, db, ["--ready", "--yes"])

    assert result.exit_code == 0, result.output
    assert calls == [first_id, second_id]
    assert "stopped at" not in result.output
    assert result.output.count("approved") == 2
    assert "gh CLI not found" in result.output

    t_first, _ = _task_state(db, first_id)
    t_second, _ = _task_state(db, second_id)
    # A skipped land never calls store.set_status(DONE) — approval is
    # recorded but the task stays awaiting_approval until a human merges
    # the PR themselves, exactly like the single-task path.
    assert t_first.status is TaskStatus.AWAITING_APPROVAL
    assert t_second.status is TaskStatus.AWAITING_APPROVAL


# --------------------------------------------------------------------------- #
# exclusion rules — a stale-sha verdict, and one whose diff touched policy    #
# --------------------------------------------------------------------------- #

def test_stale_sha_verdict_is_not_ready(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    stale_sha = "f" * 40
    task_id, _, _ = _ready_task(db, tmp_path, title="Stale", repo_name="repo",
                                mp_sha=stale_sha)

    result = _invoke(approve, db, ["--ready"])

    assert result.exit_code == 0, result.output
    assert task_id[:8] not in result.output
    assert "no awaiting_approval task is merge-ready" in result.output


def test_policy_changed_in_diff_verdict_is_not_ready(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    task_id, _, _ = _ready_task(db, tmp_path, title="Policy Changed", repo_name="repo",
                                mp_ready=True, mp_policy_changed=True)

    result = _invoke(approve, db, ["--ready"])

    assert result.exit_code == 0, result.output
    assert task_id[:8] not in result.output
    assert "no awaiting_approval task is merge-ready" in result.output


# --------------------------------------------------------------------------- #
# --ready --yes still enforces the review-pass precondition                   #
# --------------------------------------------------------------------------- #

def test_ready_yes_respects_review_pass_precondition(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    task_id, _, _ = _ready_task(db, tmp_path, title="No Review", repo_name="repo",
                                review_passed=False)

    result = _invoke(approve, db, ["--ready", "--yes"])

    assert result.exit_code == 1, result.output
    assert "step 'precondition'" in result.output

    t, _ = _task_state(db, task_id)
    assert t.status is TaskStatus.AWAITING_APPROVAL


# --------------------------------------------------------------------------- #
# mutual exclusivity refusals                                                 #
# --------------------------------------------------------------------------- #

def test_ready_with_task_id_is_rejected(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    result = _invoke(approve, db, ["--ready", "sometaskid"])

    assert result.exit_code != 0
    assert result.exit_code == 2


def test_yes_without_ready_is_rejected(tmp_path, monkeypatch):
    db = tmp_path / "nh.db"
    monkeypatch.setattr(approve_merge_mod, "land_task", _never_called_land_task)

    result = _invoke(approve, db, ["--yes"])

    assert result.exit_code != 0
    assert result.exit_code == 2


# --------------------------------------------------------------------------- #
# nh status's merge-ready: N line — the DB-only algorithm (api/models.py's    #
# merge_ready_for), distinct from --ready's live git resolution above         #
# --------------------------------------------------------------------------- #

def _status_task(db, *, title, status_, commit_sha, mp_sha, ready):
    async def _go():
        async with Store(db) as store:
            t = Task.new(title, repo_path="/tmp/x")
            t.context = {
                "merge_policy": {
                    mp_sha: {
                        "ready": ready,
                        "policy_changed_in_diff": False,
                        "rules": [dict(r) for r in _RULES],
                    },
                },
            }
            await store.create_task(t)
            aid = await store.create_attempt(t.id, 1)
            await store.update_attempt(aid, commit_sha=commit_sha)
            event = ({"source": "test", "kind": "human_merged", "text": ""}
                      if status_ is TaskStatus.DONE else None)
            await store.set_status(t, status_, validate=False, event=event)
            return t.id
    return asyncio.run(_go())


def test_status_prints_merge_ready_count(tmp_path):
    db = tmp_path / "nh.db"

    sha_ready_awaiting = "a" * 40
    _status_task(db, title="Ready Awaiting", status_=TaskStatus.AWAITING_APPROVAL,
                 commit_sha=sha_ready_awaiting, mp_sha=sha_ready_awaiting, ready=True)

    sha_ready_done = "b" * 40
    _status_task(db, title="Ready But Done", status_=TaskStatus.DONE,
                 commit_sha=sha_ready_done, mp_sha=sha_ready_done, ready=True)

    sha_live = "c" * 40
    sha_stale_verdict = "d" * 40
    _status_task(db, title="Stale Verdict Awaiting", status_=TaskStatus.AWAITING_APPROVAL,
                 commit_sha=sha_live, mp_sha=sha_stale_verdict, ready=True)

    result = _invoke(status, db, [])

    assert result.exit_code == 0, result.output
    assert "merge-ready: 1" in result.output
