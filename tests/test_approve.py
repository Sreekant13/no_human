"""A paused post-PASS task is stranded: no route back to approval without
marking it failed.

Before this fix, `nh task restore-approval` accepted only ESCALATED, FAILED
and DONE — a task paused on a `blocked` row (independent review PASSED, PR
open, resumed to IMPLEMENTING by a `pr_conflict` round and paused there
before the burn continued) had no way back to AWAITING_APPROVAL except
`nh unblock --fail` (writes a FALSE `failed` state) -> `restore-approval` ->
`approve --landed`. These tests drive the real fix: `restore-approval` now
accepts `blocked` when the EVIDENCE says so (a PASSED review + a PR), still
refuses a `blocked` task missing either, leaves the states it already
handled untouched, and the blocked -> approved path never touches `failed`.

Idiom: sync tests (each CLI command owns its own `asyncio.run`), `_bootstrap`
patched the way `tests/test_budget_terminal.py:291` does; real temp git
repos (subprocess), copied from `tests/test_landed_override.py`, for the
containment negative control and the end-to-end landed flow.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
import unittest.mock as mock

from click.testing import CliRunner

from no_human.cli.commands import approve, task_restore_approval
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus

# --------------------------------------------------------------------------- #
# git plumbing — real temp repos, no mocking of ancestry (copied from        #
# tests/test_landed_override.py)                                             #
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
    """`main`'s tip is a real ancestor of `main`, but `feature`'s tip is NOT
    — containment must refuse a `feature`-only sha as the landed commit."""
    repo = _make_repo(tmp_path, name)
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("new\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "feature: add b.txt")
    _git(repo, "checkout", "main")
    return repo


# --------------------------------------------------------------------------- #
# CLI plumbing                                                                #
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
                           lambda require_auth=False: (_cfg(db), None)):
        return CliRunner().invoke(cmd, args)


def _task_state(db, task_id):
    async def _go():
        async with Store(db) as store:
            t = await store.get_task(task_id)
            events = await store.list_events(task_id)
            return t, events
    return asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Task fixtures                                                               #
# --------------------------------------------------------------------------- #

_BLOCKER = {
    "category": "USER_PAUSED",
    "message": "paused mid pr_conflict round — burning tokens on an "
               "unresolvable conflict",
    "wake_condition": "after:2h",
    "raised_at": "2026-08-19T20:00:00+00:00",
    "confidence": 0.9,
}


async def _blocked_task_with(db, *, review_passed=1, pr=True,
                              repo_path="/tmp/x", context_extra=None,
                              title="stranded post-pass task"):
    """A task paused on `blocked` (USER_PAUSED) after a passing review and
    an open PR — the motivating incident shape. `review_passed=None` omits
    the attempt's review verdict entirely; `pr=False` omits all PR evidence
    (no `pr_watch`, no `pr_open`/`pr_draft` event)."""
    async with Store(db) as store:
        t = Task.new(title, repo_path=repo_path)
        t.context = dict(context_extra or {})
        if pr:
            t.context["pr_watch"] = "https://example.invalid/pr/9"
        await store.create_task(t)
        if pr:
            await store.save_events(t.id, [{
                "source": "test", "kind": "pr_open", "text": "", "ts": 0.0}])
        if review_passed is not None:
            aid = await store.create_attempt(t.id, 1)
            await store.update_attempt(aid, review_passed=review_passed)
        t.blocker = dict(_BLOCKER)
        t.wake_check_at = "2026-08-19T22:00:00+00:00"
        await store.update_task_columns(t)
        await store.set_status(t, TaskStatus.BLOCKED, validate=False,
                               human_override=True)
        return t.id


# --------------------------------------------------------------------------- #
# blocked + PASS + PR -> AWAITING_APPROVAL                                    #
# --------------------------------------------------------------------------- #

def test_restore_approval_accepts_a_blocked_task_with_a_passing_review(tmp_path):
    db = tmp_path / "nh.db"
    tid = asyncio.run(_blocked_task_with(db, review_passed=1, pr=True))

    result = _invoke(task_restore_approval, db,
                     [tid, "--reason", "conflict round paused; review already passed"])

    assert result.exit_code == 0, result.output

    t, events = _task_state(db, tid)
    assert t.status is TaskStatus.AWAITING_APPROVAL
    assert t.blocker is None
    assert t.wake_check_at is None
    repaired = [e for e in events if e.get("kind") == "human_restore_approval"]
    assert len(repaired) == 1, events
    text = repaired[0]["text"]
    assert "https://example.invalid/pr/9" in text
    assert "PASS" in text
    assert "blocked" in text


# --------------------------------------------------------------------------- #
# refusals — negative controls, each naming the missing precondition          #
# --------------------------------------------------------------------------- #

def test_blocked_without_a_passing_review_is_refused(tmp_path):
    db = tmp_path / "nh.db"
    tid = asyncio.run(_blocked_task_with(db, review_passed=0, pr=True))

    result = _invoke(task_restore_approval, db, [tid])

    assert result.exit_code == 1, result.output
    assert "review" in result.output.lower()
    assert "fail" in result.output.lower()

    t, events = _task_state(db, tid)
    assert t.status is TaskStatus.BLOCKED
    assert not any(e.get("kind") == "human_restore_approval" for e in events), events


def test_blocked_with_no_review_verdict_is_refused(tmp_path):
    db = tmp_path / "nh.db"
    tid = asyncio.run(_blocked_task_with(db, review_passed=None, pr=True))

    result = _invoke(task_restore_approval, db, [tid])

    assert result.exit_code == 1, result.output
    assert "review" in result.output.lower()

    t, events = _task_state(db, tid)
    assert t.status is TaskStatus.BLOCKED
    assert not any(e.get("kind") == "human_restore_approval" for e in events), events


def test_blocked_without_pr_evidence_is_refused(tmp_path):
    db = tmp_path / "nh.db"
    tid = asyncio.run(_blocked_task_with(db, review_passed=1, pr=False))

    result = _invoke(task_restore_approval, db, [tid])

    assert result.exit_code == 1, result.output
    assert "pr" in result.output.lower()

    t, events = _task_state(db, tid)
    assert t.status is TaskStatus.BLOCKED
    assert not any(e.get("kind") == "human_restore_approval" for e in events), events


# --------------------------------------------------------------------------- #
# the states restore-approval already handled are unchanged                   #
# --------------------------------------------------------------------------- #

def test_escalated_and_failed_and_done_paths_are_unchanged(tmp_path):
    db = tmp_path / "nh.db"

    # (a) escalated + pr_watch + pr_open -> restored, exactly as before.
    async def _setup_escalated():
        async with Store(db) as store:
            t = Task.new("escalated with an open PR", repo_path="/tmp/x")
            t.context = {"pr_watch": "https://example.invalid/pr/1"}
            await store.create_task(t)
            await store.save_events(t.id, [{
                "source": "test", "kind": "pr_open", "text": "", "ts": 0.0}])
            await store.set_status(t, TaskStatus.ESCALATED, validate=False,
                                   human_override=True)
            return t.id
    esc_id = asyncio.run(_setup_escalated())
    esc_result = _invoke(task_restore_approval, db, [esc_id, "--reason", "probe"])
    assert esc_result.exit_code == 0, esc_result.output
    esc_task, _ = _task_state(db, esc_id)
    assert esc_task.status is TaskStatus.AWAITING_APPROVAL

    # (b) failed + cancel_reason -> refused, "cancelled by a human".
    async def _setup_cancelled():
        async with Store(db) as store:
            t = Task.new("cancelled with an old PR", repo_path="/tmp/x")
            t.context = {"pr_watch": "https://example.invalid/pr/2",
                         "cancel_reason": "Cancelled from board"}
            await store.create_task(t)
            await store.save_events(t.id, [{
                "source": "test", "kind": "pr_open", "text": "", "ts": 0.0}])
            await store.set_status(t, TaskStatus.FAILED, validate=False,
                                   human_override=True)
            return t.id
    failed_id = asyncio.run(_setup_cancelled())
    failed_result = _invoke(task_restore_approval, db, [failed_id, "--reason", "probe"])
    assert failed_result.exit_code == 1, failed_result.output
    assert "cancelled by a human" in failed_result.output
    failed_task, _ = _task_state(db, failed_id)
    assert failed_task.status is TaskStatus.FAILED

    # (c) done + a completion event -> refused, "completion event on record".
    async def _setup_done():
        async with Store(db) as store:
            t = Task.new("legitimately done", repo_path="/tmp/x")
            await store.create_task(t)
            await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
            await store.set_status(
                t, TaskStatus.DONE, validate=False,
                event={"source": "human", "kind": "human_merged",
                       "sha": "a" * 40, "ts": time.time()},
            )
            return t.id
    done_id = asyncio.run(_setup_done())
    done_result = _invoke(task_restore_approval, db, [done_id])
    assert done_result.exit_code == 1, done_result.output
    assert "completion event on record" in done_result.output
    done_task, _ = _task_state(db, done_id)
    assert done_task.status is TaskStatus.DONE


# --------------------------------------------------------------------------- #
# ESCALATED/FAILED tail now reads the canonical PR predicate                  #
# (task_has_pr_evidence), not ctx["pr_watch"] directly                        #
# --------------------------------------------------------------------------- #

def test_escalated_task_with_a_pr_only_in_the_event_log_is_restored(tmp_path):
    """The worker died between the review passing and the PR being written
    back to task context: no `pr_watch`, no attempt `pr_url`, only a
    `pr_open` event on record. `task_has_pr_evidence` resolves this; a
    direct `ctx["pr_watch"]` read does not.

    Mutation to state in the PR body: revert the gate to
    `if not (t.context or {}).get("pr_watch")` -> this test fails with
    exit_code == 1 and output containing "task has no open PR".
    """
    db = tmp_path / "nh.db"

    async def _setup():
        async with Store(db) as store:
            t = Task.new("escalated, PR only in the event log", repo_path="/tmp/x")
            t.context = {}
            await store.create_task(t)
            await store.save_events(t.id, [{
                "source": "test", "kind": "pr_open",
                "text": "https://example.invalid/pr/480", "ts": 0.0}])
            await store.set_status(t, TaskStatus.ESCALATED, validate=False,
                                   human_override=True)
            return t.id
    tid = asyncio.run(_setup())

    result = _invoke(task_restore_approval, db, [tid, "--reason", "probe"])

    assert result.exit_code == 0, result.output
    t, events = _task_state(db, tid)
    assert t.status is TaskStatus.AWAITING_APPROVAL
    repaired = [e for e in events if e.get("kind") == "human_restore_approval"]
    assert len(repaired) == 1, events
    assert (t.context or {}).get("pr_closed_repaired_url") == \
        "https://example.invalid/pr/480"


def test_escalated_task_with_no_pr_anywhere_is_still_refused(tmp_path):
    db = tmp_path / "nh.db"

    async def _setup():
        async with Store(db) as store:
            t = Task.new("escalated, no PR anywhere", repo_path="/tmp/x")
            t.context = {}
            await store.create_task(t)
            await store.set_status(t, TaskStatus.ESCALATED, validate=False,
                                   human_override=True)
            return t.id
    tid = asyncio.run(_setup())

    result = _invoke(task_restore_approval, db, [tid, "--reason", "probe"])

    assert result.exit_code == 1, result.output
    assert "has no open PR" in result.output
    normalized = " ".join(result.output.split())
    assert "restore-approval only repairs parked-PR tasks" in normalized
    t, events = _task_state(db, tid)
    assert t.status is TaskStatus.ESCALATED
    assert not any(e.get("kind") == "human_restore_approval" for e in events), events


def test_escalated_task_whose_only_pr_is_abandoned_is_refused(tmp_path):
    """The abandonment subtraction lives inside `task_has_pr_evidence` — the
    event is a `pr_open` (so the separate `pr_open`-event gate at :1724
    would otherwise pass), meaning this refusal can only come from
    `task_has_pr_evidence` itself excluding the abandoned URL."""
    db = tmp_path / "nh.db"
    url = "https://example.invalid/pr/253"

    async def _setup():
        async with Store(db) as store:
            t = Task.new("escalated, only PR is abandoned", repo_path="/tmp/x")
            t.context = {"pr_draft_created": url, "abandoned_pr_urls": [url]}
            await store.create_task(t)
            await store.save_events(t.id, [{
                "source": "test", "kind": "pr_open", "text": url, "ts": 0.0}])
            await store.set_status(t, TaskStatus.ESCALATED, validate=False,
                                   human_override=True)
            return t.id
    tid = asyncio.run(_setup())

    result = _invoke(task_restore_approval, db, [tid, "--reason", "probe"])

    assert result.exit_code == 1, result.output
    assert "has no open PR" in result.output
    t, events = _task_state(db, tid)
    assert t.status is TaskStatus.ESCALATED
    assert not any(e.get("kind") == "human_restore_approval" for e in events), events


# --------------------------------------------------------------------------- #
# no path requires `failed`, and containment still runs                       #
# --------------------------------------------------------------------------- #

def test_blocked_to_landed_never_passes_through_failed(tmp_path):
    db = tmp_path / "nh.db"
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "main")

    tid = asyncio.run(_blocked_task_with(
        db, review_passed=1, pr=True, repo_path=str(repo),
        context_extra={"base_branch": "main"}))

    restore_result = _invoke(task_restore_approval, db,
                             [tid, "--reason", "conflict round paused"])
    assert restore_result.exit_code == 0, restore_result.output

    t, _ = _task_state(db, tid)
    assert t.status is TaskStatus.AWAITING_APPROVAL

    landed_result = _invoke(
        approve, db, [tid, "--landed", sha, "--because", "hand-verified landing"])
    assert landed_result.exit_code == 0, landed_result.output
    assert "residue:" in landed_result.output

    final, events = _task_state(db, tid)
    assert final.status is TaskStatus.DONE

    # The task must NEVER have passed through `failed` on the way there.
    for e in events:
        assert e.get("kind") != "failed"
        text = str(e.get("text") or "")
        assert TaskStatus.FAILED.value not in text, e


def test_approve_landed_still_refuses_a_sha_not_in_the_base_branch(tmp_path):
    db = tmp_path / "nh.db"
    repo = _repo_with_residue(tmp_path)
    off_branch_sha = _git_out(repo, "rev-parse", "feature")

    tid = asyncio.run(_blocked_task_with(
        db, review_passed=1, pr=True, repo_path=str(repo),
        context_extra={"base_branch": "main"}))

    restore_result = _invoke(task_restore_approval, db,
                             [tid, "--reason", "conflict round paused"])
    assert restore_result.exit_code == 0, restore_result.output

    landed_result = _invoke(
        approve, db,
        [tid, "--landed", off_branch_sha, "--because", "wrong sha"])

    assert landed_result.exit_code == 1, landed_result.output
    assert "is not an ancestor of" in landed_result.output

    t, _ = _task_state(db, tid)
    assert t.status is TaskStatus.AWAITING_APPROVAL


# --------------------------------------------------------------------------- #
# the "pending_never_ran" shape — hand-landed before any coder attempt ran,   #
# live incident 855f1263: `nh approve --landed` refused PENDING outright,    #
# then refused a second time on "no recorded base_branch".                   #
# --------------------------------------------------------------------------- #

async def _pending_task(db, *, repo_path):
    async with Store(db) as store:
        t = Task.new("hand-landed before any attempt ran", repo_path=repo_path)
        t.context = {}
        await store.create_task(t)
        return t.id


def test_approve_landed_completes_a_pending_task_that_never_ran(tmp_path):
    db = tmp_path / "nh.db"
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "main")
    tid = asyncio.run(_pending_task(db, repo_path=str(repo)))

    landed_result = _invoke(
        approve, db,
        [tid, "--landed", sha,
         "--because", "landed by the supervising session",
         "--base", "main"])

    assert landed_result.exit_code == 0, landed_result.output
    assert "residue:" in landed_result.output

    t, events = _task_state(db, tid)
    assert t.status is TaskStatus.DONE
    override_events = [e for e in events
                       if e.get("kind") == "approved_landed_override"]
    assert len(override_events) == 1, events
    assert override_events[0]["shape"] == "pending_never_ran"
    assert override_events[0]["base_source"] == "human_asserted"
    assert (t.context or {}).get("base_branch") == "main"


def test_approve_landed_pending_requires_because(tmp_path):
    db = tmp_path / "nh.db"
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "main")
    tid = asyncio.run(_pending_task(db, repo_path=str(repo)))

    landed_result = _invoke(approve, db, [tid, "--landed", sha])

    assert landed_result.exit_code == 1, landed_result.output

    t, events = _task_state(db, tid)
    assert t.status is TaskStatus.PENDING
    assert not any(
        e.get("kind") == "approved_landed_override" for e in events), events


def test_approve_landed_pending_requires_explicit_base(tmp_path):
    """F1: `--because` alone is not enough for a pending task that never
    recorded a base_branch — `--base` is required too, and this tool never
    guesses one (see blockers/landed_override.py's module docstring)."""
    db = tmp_path / "nh.db"
    repo = _make_repo(tmp_path)
    sha = _git_out(repo, "rev-parse", "main")
    tid = asyncio.run(_pending_task(db, repo_path=str(repo)))

    landed_result = _invoke(
        approve, db,
        [tid, "--landed", sha, "--because", "landed by the supervising session"])

    assert landed_result.exit_code == 1, landed_result.output
    assert "--base" in landed_result.output

    t, events = _task_state(db, tid)
    assert t.status is TaskStatus.PENDING
    assert not any(
        e.get("kind") == "approved_landed_override" for e in events), events
