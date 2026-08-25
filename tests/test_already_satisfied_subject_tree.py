"""Already-satisfied claims are reviewed only on delivery trees."""

from __future__ import annotations

import subprocess

import pytest

from no_human.agent.claude_backend import AgentResult
from no_human.config import load_config
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.review.reviewer import ReviewDecision
from no_human.review.selfcheck import ChecklistItem
from no_human.vcs import GitRepo


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


@pytest.fixture
def bare_repo(tmp_path):
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@example.test")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "initial")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


@pytest.fixture
async def store(tmp_path):
    result = await Store(tmp_path / "nh.db").connect()
    yield result
    await result.close()


def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("blockers", {})["challenge"] = False
    return cfg


class AlreadySatisfiedBackend:
    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        return AgentResult(final_text=CLAIM, num_turns=1, is_error=False,
                           tokens_used=1, session_id="s", stop_reason="end_turn")


class FakeReviewer:
    def __init__(self, decision):
        self.decision = decision
        self.calls: list[dict] = []

    async def review(self, task, *, repo_path, **kwargs):
        self.calls.append({"mode": kwargs.get("mode")})
        return self.decision


CLAIM = "ALREADY-SATISFIED\nCRITERION: existing — MET — evidence: calc.py:1\n"
PASS = ReviewDecision(passed=True, checklist=[
    ChecklistItem("existing", True, "calc.py:1")])
FAIL = ReviewDecision(passed=False, checklist=[
    ChecklistItem("existing", False, "calc.py is not enough", severity="high")])


async def _gate(store, tmp_path, repo_path, *, branch, reviewer=PASS):
    events: list[dict] = []
    fake = FakeReviewer(reviewer)
    orch = Orchestrator(store, _config(tmp_path).data, AlreadySatisfiedBackend(),
                        SlackNotifier(None), reviewer=fake, event_sink=events.append)
    task = Task.new("existing", repo_path=str(repo_path), kind="feature")
    task.acceptance_criteria = ["existing"]
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)
    outcome = await orch._gate_already_satisfied(
        task, GitRepo(repo_path), attempt_id, CLAIM, branch=branch,
        attempt_n=1, base="main")
    return outcome, task, fake, events


async def test_a_wip_blocked_non_ancestor_unpushed_claim_is_refused(
    bare_repo, tmp_path, store
):
    branch = "no-human/wip"
    _git(bare_repo, "checkout", "-b", branch)
    (bare_repo / "wip.txt").write_text("not shipped\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "[WIP-BLOCKED] unfinished")
    sha = GitRepo(bare_repo).head_sha()
    assert subprocess.run(["git", "merge-base", "--is-ancestor", sha, "origin/main"],
                          cwd=bare_repo).returncode != 0

    outcome, task, reviewer, events = await _gate(
        store, tmp_path, bare_repo, branch=branch)

    assert outcome.status is TaskStatus.FAILED
    assert reviewer.calls == []
    refreshed = await store.find_task(task.id)
    assert refreshed.status is not TaskStatus.AWAITING_APPROVAL
    assert "already_satisfied_report" not in (refreshed.context or {})
    assert not (refreshed.context or {}).get("review_history")
    attempts = await store.list_attempts(task.id)
    assert sha in attempts[-1]["failure_reason"]
    assert "origin/main" in attempts[-1]["failure_reason"]
    assert any(event["kind"] == "already_satisfied_unshippable" for event in events)


async def test_a_claim_whose_work_is_on_main_still_passes(bare_repo, tmp_path, store):
    reviewer = FakeReviewer(PASS)
    orch = Orchestrator(store, _config(tmp_path).data, AlreadySatisfiedBackend(),
                        SlackNotifier(None), reviewer=reviewer)
    task = Task.new("existing", repo_path=str(bare_repo), kind="feature")
    task.acceptance_criteria = ["existing"]
    task.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(task)
    outcome = await orch.run_task(task)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert reviewer.calls[-1]["mode"] == "already_satisfied"
    refreshed = await store.find_task(task.id)
    assert refreshed.context["already_satisfied_report"] == CLAIM.strip()
    assert refreshed.context["review_history"][-1]["sha"] == GitRepo(bare_repo).head_sha()


async def test_a_pushed_branch_tip_equal_to_the_subject_sha_is_accepted(
    bare_repo, tmp_path, store
):
    branch = "no-human/pushed"
    _git(bare_repo, "checkout", "-b", branch)
    (bare_repo / "pushed.txt").write_text("off main\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "work")
    _git(bare_repo, "push", "-u", "origin", branch)

    outcome, _, reviewer, events = await _gate(store, tmp_path, bare_repo, branch=branch)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert reviewer.calls
    assert "pushed branch" in outcome.detail
    assert "not on origin/main" in outcome.detail
    evidence = next(event for event in events if event["kind"] == "already_satisfied")
    assert evidence["subject_on_main"] is False


@pytest.mark.parametrize("value, phrase", [("", "empty"), (None, "unresolvable")])
async def test_absent_or_empty_head_sha_is_a_refusal(
    bare_repo, tmp_path, store, monkeypatch, value, phrase
):
    if value is None:
        monkeypatch.setattr(GitRepo, "head_sha", lambda self: (_ for _ in ()).throw(
            RuntimeError("cannot read HEAD")))
    else:
        monkeypatch.setattr(GitRepo, "head_sha", lambda self: value)

    outcome, task, reviewer, _ = await _gate(store, tmp_path, bare_repo, branch="main")

    assert outcome.status is TaskStatus.FAILED
    assert reviewer.calls == []
    assert phrase in (await store.list_attempts(task.id))[-1]["failure_reason"]


@pytest.mark.parametrize("relation", ["behind", "diverged"])
async def test_a_diverged_and_a_behind_remote_tip_are_both_refused(
    bare_repo, tmp_path, store, relation
):
    branch = "no-human/relation"
    _git(bare_repo, "checkout", "-b", branch)
    (bare_repo / "work.txt").write_text("work\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "work")
    _git(bare_repo, "push", "-u", "origin", branch)
    if relation == "behind":
        (bare_repo / "remote.txt").write_text("remote only\n")
        _git(bare_repo, "add", "-A")
        _git(bare_repo, "commit", "-m", "remote only")
        _git(bare_repo, "push", "origin", branch)
        _git(bare_repo, "reset", "--hard", "HEAD~1")
    else:
        _git(bare_repo, "commit", "--amend", "-m", "work (rewritten)")

    outcome, _, reviewer, _ = await _gate(store, tmp_path, bare_repo, branch=branch)

    assert outcome.status is TaskStatus.FAILED
    assert reviewer.calls == []


async def test_an_unknown_pushed_branch_relation_is_refused(
    bare_repo, tmp_path, store, monkeypatch
):
    branch = "no-human/unknown-relation"
    _git(bare_repo, "checkout", "-b", branch)
    (bare_repo / "work.txt").write_text("work\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "work")
    _git(bare_repo, "push", "-u", "origin", branch)
    monkeypatch.setattr(GitRepo, "remote_branch_relation", lambda self, branch: "unknown")

    outcome, _, reviewer, _ = await _gate(store, tmp_path, bare_repo, branch=branch)

    assert outcome.status is TaskStatus.FAILED
    assert reviewer.calls == []


async def test_a_wip_blocked_subject_is_refused_even_when_pushed(bare_repo, tmp_path, store):
    branch = "no-human/pushed-wip"
    _git(bare_repo, "checkout", "-b", branch)
    (bare_repo / "wip.txt").write_text("unfinished\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "[WIP-BLOCKED] unfinished")
    _git(bare_repo, "push", "-u", "origin", branch)

    outcome, _, reviewer, _ = await _gate(store, tmp_path, bare_repo, branch=branch)

    assert outcome.status is TaskStatus.FAILED
    assert reviewer.calls == []


async def test_no_commit_sha_and_no_pushed_branch_can_never_reach_awaiting_approval(
    bare_repo, tmp_path, store
):
    branch = "no-human/no-delivery"
    _git(bare_repo, "checkout", "-b", branch)
    (bare_repo / "unshipped.txt").write_text("unshipped\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "work")

    outcome, task, _, events = await _gate(store, tmp_path, bare_repo, branch=branch)

    assert outcome.status is TaskStatus.FAILED
    attempt = (await store.list_attempts(task.id))[-1]
    assert not attempt["commit_sha"]
    remote = _git(bare_repo, "ls-remote", "origin", f"refs/heads/{branch}")
    assert not remote.stdout.strip()
    assert not any(event["kind"] == "already_satisfied" for event in events)
    assert (await store.find_task(task.id)).status is not TaskStatus.AWAITING_APPROVAL


async def test_an_unreachable_remote_is_a_refusal(bare_repo, tmp_path, store):
    branch = "no-human/offline"
    _git(bare_repo, "checkout", "-b", branch)
    (bare_repo / "work.txt").write_text("work\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "work")
    _git(bare_repo, "remote", "set-url", "origin", "/nonexistent/remote.git")

    outcome, _, reviewer, _ = await _gate(store, tmp_path, bare_repo, branch=branch)

    assert outcome.status is TaskStatus.FAILED
    assert reviewer.calls == []


async def test_confirmation_detail_names_the_verified_tree(bare_repo, tmp_path, store):
    outcome, task, _, events = await _gate(store, tmp_path, bare_repo, branch="main")
    sha = GitRepo(bare_repo).head_sha()[:12]

    assert sha in outcome.detail
    assert "on origin/main" in outcome.detail
    evidence = next(event for event in events if event["kind"] == "already_satisfied")
    assert evidence["subject_on_main"] is True
    assert (await store.find_task(task.id)).status is TaskStatus.AWAITING_APPROVAL


async def test_a_pushed_pass_stamp_still_does_not_cover_a_later_rewrite(
    bare_repo, tmp_path, store
):
    branch = "no-human/pushed-rewrite"
    _git(bare_repo, "checkout", "-b", branch)
    (bare_repo / "work.txt").write_text("work\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "work")
    _git(bare_repo, "push", "-u", "origin", branch)
    reviewed_sha = GitRepo(bare_repo).head_sha()

    outcome, task, reviewer, _ = await _gate(
        store, tmp_path, bare_repo, branch=branch)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert reviewer.calls
    history = ((await store.find_task(task.id)).context or {}).get("review_history") or []
    assert history[-1]["sha"] == reviewed_sha

    _git(bare_repo, "commit", "--amend", "-m", "work (rewritten)")
    rewritten_sha = GitRepo(bare_repo).head_sha()

    assert rewritten_sha != reviewed_sha
    assert not GitRepo(bare_repo).is_ancestor(reviewed_sha, rewritten_sha)
    assert history[-1]["sha"] != rewritten_sha


async def test_refusal_and_refutation_share_the_failed_feedback_shape(
    bare_repo, tmp_path, store
):
    failed, failed_task, _, _ = await _gate(
        store, tmp_path, bare_repo, branch="main", reviewer=FAIL)
    failed_context = (await store.find_task(failed_task.id)).context or {}

    branch = "no-human/unpushed"
    _git(bare_repo, "checkout", "-b", branch)
    (bare_repo / "unshipped.txt").write_text("unshipped\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "work")
    refused, refused_task, _, _ = await _gate(store, tmp_path, bare_repo, branch=branch)
    refused_context = (await store.find_task(refused_task.id)).context or {}

    assert failed.status is refused.status is TaskStatus.FAILED
    assert failed_context["review_round_seq"] == refused_context["review_round_seq"] == 1
    assert len(failed_context["review_feedback"]) == len(
        refused_context["review_feedback"]) == 1
