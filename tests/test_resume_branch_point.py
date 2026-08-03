"""The bounded loop must branch each attempt from the NEWEST checkpoint.

Found by root-causing task afe1ed12 ("nh repo new"), which a friend filed and
which burned 12,071,981 tokens across 4 attempts without ever opening a PR. Its
event log showed the same branch point four times:

    20:14:27  checkpoint  WIP-BLOCKED  3f55ce6c
    20:17:31  resume_wip  branching from WIP-BLOCKED 3f55ce6c
    20:20:58  checkpoint  WIP-PARTIAL  b6f55e6c   <- attempt's work
    20:20:58  resume_wip  branching from WIP-BLOCKED 3f55ce6c   <- discarded it
    20:24:07  checkpoint  WIP-PARTIAL  9cd6f750   <- and again
    20:24:20  resume_wip  branching from WIP-BLOCKED 3f55ce6c   <- discarded it

Two independent defects produced that:
  1. `resume_from` outranked `handoff.wip_sha` unconditionally, so once a task
     had been resumed every later attempt re-branched from the blocked commit.
  2. The budget/stuck/timeout abort paths committed [WIP-PARTIAL] but never
     recorded the sha anywhere the next attempt reads, so even without (1) the
     work was unreachable. The live task proves it: three WIP-PARTIAL commits
     were made and ctx["handoff"]["wip_sha"] was still empty.

Each attempt therefore redid the same exploration from the same starting point
and the loop could not converge.
"""
import subprocess

import pytest

from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.vcs.git import GitRepo


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def chain(tmp_path):
    """base -> resume_point -> wip, plus `orphan` on an unrelated branch."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "a.py").write_text("base\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base")
    base = _git(work, "rev-parse", "HEAD")

    (work / "a.py").write_text("blocked checkpoint\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "[WIP-BLOCKED] work")
    resume_point = _git(work, "rev-parse", "HEAD")

    (work / "b.py").write_text("tens of turns of partial work\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "[WIP-PARTIAL] work")
    wip = _git(work, "rev-parse", "HEAD")

    # A commit that does NOT descend from resume_point — e.g. a handoff left
    # over from before the human answered, which the answer redirected away from.
    _git(work, "checkout", "-q", base)
    _git(work, "checkout", "-q", "-b", "unrelated")
    (work / "c.py").write_text("abandoned direction\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "[WIP-PARTIAL] abandoned")
    orphan = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "-q", "main")

    return {"repo": GitRepo(work), "base": base,
            "resume_point": resume_point, "wip": wip, "orphan": orphan}


@pytest.fixture
def orch():
    """Only the branch-point decision is under test — it reads no collaborators."""
    return Orchestrator.__new__(Orchestrator)


def test_ancestor_of_is_directional(orch, chain):
    """Positive AND negative control: without both, a guard that always returns
    False (or always True) would look correct."""
    repo, r, wip = chain["repo"], chain["resume_point"], chain["wip"]
    assert orch._ancestor_of(repo, r, wip) is True          # positive control
    assert orch._ancestor_of(repo, wip, r) is False         # direction matters
    assert orch._ancestor_of(repo, r, chain["orphan"]) is False


def test_ancestor_of_fails_closed_on_unknown_shas(orch, chain):
    repo = chain["repo"]
    assert orch._ancestor_of(repo, chain["resume_point"], "0" * 40) is False
    assert orch._ancestor_of(repo, "", chain["wip"]) is False
    assert orch._ancestor_of(repo, chain["resume_point"], "") is False


def test_attempt_two_prefers_the_previous_attempts_partial_work(orch, chain):
    """THE REGRESSION. Attempt 2 of a resumed run must branch from the
    [WIP-PARTIAL] its predecessor just wrote, not the older [WIP-BLOCKED]."""
    ctx = {"resume_from": {"sha": chain["resume_point"]},
           "handoff": {"wip_sha": chain["wip"]}}
    assert orch._resume_branch_point(chain["repo"], ctx, attempt_n=2) == chain["wip"]


def test_attempt_one_still_resumes_from_the_blocked_checkpoint(orch, chain):
    """D15 must not regress: attempt 1 of a resumed run has no predecessor of
    its own, so the human's resume point is still the right branch base."""
    ctx = {"resume_from": {"sha": chain["resume_point"]},
           "handoff": {"wip_sha": chain["wip"]}}
    assert orch._resume_branch_point(
        chain["repo"], ctx, attempt_n=1) == chain["resume_point"]


def test_partial_work_off_the_resume_line_is_rejected(orch, chain):
    """A handoff that does not descend from the resume point may predate the
    human's answer; the answer wins."""
    ctx = {"resume_from": {"sha": chain["resume_point"]},
           "handoff": {"wip_sha": chain["orphan"]}}
    assert orch._resume_branch_point(
        chain["repo"], ctx, attempt_n=2) == chain["resume_point"]


def _lose_a_commit(work) -> str:
    """A real commit, then really pruned — the shape a `git gc` or a history
    rewrite leaves behind. A hand-written fake sha would not prove the decision
    consults the object store; the `cat-file -e` assertion is the probe that the
    positive case really became negative. Everything else in `chain` stays
    reachable through `main` and `unrelated`, so only this commit goes."""
    _git(work, "checkout", "-q", "-b", "gone")
    (work / "gated.py").write_text("the commit a human gated\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "[WIP-BLOCKED] gated")
    sha = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "-q", "main")
    _git(work, "branch", "-D", "gone")
    _git(work, "reflog", "expire", "--expire=now", "--all")
    _git(work, "gc", "--prune=now", "--quiet")
    assert subprocess.run(["git", "cat-file", "-e", sha], cwd=work,
                          capture_output=True).returncode != 0, \
        "the resume point must really be gone, or this test proves nothing"
    return sha


def test_a_vanished_resume_point_does_not_veto_the_partial_that_survived(
        orch, chain):
    """THE DATA LOSS. Nothing pushes a checkpoint, so a pruned resume point is
    routine — and `_ancestor_of` fails CLOSED, which on an unreadable ancestor
    is not caution but a veto cast by a commit that no longer exists. It
    rejected the [WIP-PARTIAL] the previous attempt paid tens of turns for, won
    `candidate or resume_sha`, and sent the attempt back to base; the caller
    then announced the loss of the commit it could not read and said nothing
    about the work it could."""
    work = chain["repo"].path
    gone = _lose_a_commit(work)
    ctx = {"resume_from": {"sha": gone, "by": "human"},
           "handoff": {"wip_sha": chain["wip"]}}
    assert orch._resume_branch_point(chain["repo"], ctx, attempt_n=2) == chain["wip"]


def test_a_vanished_resume_point_still_reaches_the_caller_to_be_announced(
        orch, chain):
    """The loud fallback must stay reachable. With no partial to fall back to
    (attempt 1 of a run has none), the vanished sha is still what this returns —
    `_run_attempt` needs it to name the loss it is about to report."""
    work = chain["repo"].path
    gone = _lose_a_commit(work)
    ctx = {"resume_from": {"sha": gone, "by": "human"},
           "handoff": {"wip_sha": chain["wip"]}}
    assert orch._resume_branch_point(chain["repo"], ctx, attempt_n=1) == gone
    assert orch._resume_branch_point(
        chain["repo"], {"resume_from": {"sha": gone}}, attempt_n=2) == gone


def test_a_vanished_resume_point_cannot_veto_on_lineage_it_cannot_prove(
        orch, chain):
    """The policy, stated executably. `orphan` does NOT descend from the resume
    point, and while that point is READABLE the check rejects it — see
    `test_partial_work_off_the_resume_line_is_rejected`, the control for this.
    Once the point is gone the ancestry question is unanswerable, and the choice
    is no longer "resume point vs partial" (nothing can branch onto a commit the
    object store lacks) but "partial vs base". Base discards work that exists,
    so the partial wins; the caller announces the substitution and names both
    shas."""
    work = chain["repo"].path
    gone = _lose_a_commit(work)
    ctx = {"resume_from": {"sha": gone, "by": "human"},
           "handoff": {"wip_sha": chain["orphan"]}}
    assert orch._resume_branch_point(
        chain["repo"], ctx, attempt_n=2) == chain["orphan"]


def test_unresumed_run_still_uses_its_own_partial_work(orch, chain):
    """No resume_from at all (the ordinary retry loop) — unchanged behaviour."""
    ctx = {"handoff": {"wip_sha": chain["wip"]}}
    assert orch._resume_branch_point(chain["repo"], ctx, attempt_n=2) == chain["wip"]
    assert orch._resume_branch_point(chain["repo"], ctx, attempt_n=1) == ""


def test_no_checkpoints_means_branch_from_base(orch, chain):
    assert orch._resume_branch_point(chain["repo"], {}, attempt_n=3) == ""


async def test_abort_paths_record_the_checkpoint_for_the_next_attempt(tmp_path):
    """The budget/stuck/timeout aborts commit [WIP-PARTIAL] but have no
    AgentResult, so they cannot use _persist_handoff. They must still record the
    sha, or the commit is unreachable — which is what left the live task's
    handoff.wip_sha empty after three WIP-PARTIAL commits.
    """
    from no_human.core.db import Store

    store = await Store(tmp_path / "nh.db").connect()
    try:
        orch = Orchestrator.__new__(Orchestrator)
        orch.store = store
        t = Task.new("t", repo_path=str(tmp_path))
        await store.create_task(t)

        await orch._record_wip_checkpoint(t, "a" * 40,
                                          stopped_because="stuck-abort")
        assert (await store.get_task(t.id)).context["handoff"]["wip_sha"] == "a" * 40

        # An earlier attempt's narrative must survive the merge.
        t2 = await store.get_task(t.id)
        ctx = t2.context
        ctx["handoff"]["summary"] = "what attempt 1 accomplished"
        t2.context = ctx
        await store.update_task(t2)
        await orch._record_wip_checkpoint(t2, "b" * 40,
                                          stopped_because="attempt timeout")
        handoff = (await store.get_task(t.id)).context["handoff"]
        assert handoff["wip_sha"] == "b" * 40
        assert handoff["summary"] == "what attempt 1 accomplished"

        # Nothing to record must not clobber a real checkpoint.
        await orch._record_wip_checkpoint(await store.get_task(t.id), "", stopped_because="stuck-abort")
        assert (await store.get_task(t.id)).context["handoff"]["wip_sha"] == "b" * 40
    finally:
        await store.close()
