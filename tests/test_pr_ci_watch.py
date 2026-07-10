"""The awaiting-approval ladder: merged → done, closed → escalate, red CI →
bounded fix loop. Born from PR #531: the Jenkinsfile died in Jenkins' CPS
compiler (MethodTooLargeException) while every local check passed, and nothing
watched the PR's own pipeline."""

from __future__ import annotations

import pytest

from no_human.blockers.wake import WakeWatcher
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


async def _approval_task(store, url="https://code.example.com/dev/x/pull/531"):
    t = Task.new("ci_gate", repo_path="/tmp/x")
    t.context = {"pr_watch": url, "pr_branch": "scratch/x"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    return t


def _watcher(store, *, state="OPEN", checks=None, log="", events=None):
    async def pr_state(url): return state
    async def pr_checks(url): return checks or []
    async def ci_log(link): return log
    async def pr_comment(url): return []
    return WakeWatcher(
        store, {}, pr_state=pr_state, pr_checks=pr_checks, ci_log=ci_log,
        pr_comment=pr_comment,
        on_event=(lambda k, t: events.append((k, t))) if events is not None else None,
    )


FAIL_CHECK = {
    "name": "continuous-integration/jenkins/pr-head", "status": "fail",
    "link": "https://build.example.com/.../PR-531/2/display/redirect",
}


async def test_merged_pr_flips_the_task_to_done(store):
    t = await _approval_task(store)
    events = []
    out = await _watcher(store, state="MERGED", events=events)._check_open_pr(t)
    assert out == "merged"
    assert (await store.get_task(t.id)).status is TaskStatus.DONE
    assert any(k == "merged" for k, _ in events)


async def test_closed_unmerged_escalates_with_a_question(store):
    t = await _approval_task(store)
    out = await _watcher(store, state="CLOSED")._check_open_pr(t)
    assert out == "escalated_pr_closed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED
    assert "closed without merging" in (fresh.blocker or {}).get("question", "")


async def test_red_ci_injects_the_log_and_resumes_onto_the_pr_branch(store):
    t = await _approval_task(store)
    events = []
    w = _watcher(store, checks=[FAIL_CHECK],
                 log="Method too large: WorkflowScript.___cps___", events=events)
    out = await w._check_open_pr(t)
    assert out == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    fb = fresh.context["send_back_feedback"]
    assert fb[-1]["source"] == "pr_ci"
    assert "Method too large" in fb[-1]["message"]
    assert fresh.context["pr_ci_rounds"] == 1
    assert any(k == "pr_ci_red" for k, _ in events)


async def test_the_same_failure_signature_never_burns_a_second_round(store):
    """A re-run (or re-poll) of the same red check must be free."""
    t = await _approval_task(store)
    w = _watcher(store, checks=[FAIL_CHECK])
    assert await w._check_open_pr(t) == "resumed"
    t = await store.get_task(t.id)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    assert await w._check_open_pr(t) is None
    assert (await store.get_task(t.id)).context["pr_ci_rounds"] == 1


async def test_a_new_build_failing_the_same_checks_is_a_new_round(store):
    """The fix push re-ran CI (new build number in the link) and the same
    checks failed again — that must inject fresh feedback, not read as
    "already handled". Signing on names alone deadlocked exactly here."""
    t = await _approval_task(store)
    assert await _watcher(store, checks=[FAIL_CHECK])._check_open_pr(t) == "resumed"
    rerun = {**FAIL_CHECK, "link": FAIL_CHECK["link"].replace("/2/", "/3/")}
    t = await store.get_task(t.id)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    assert await _watcher(store, checks=[rerun])._check_open_pr(t) == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.context["pr_ci_rounds"] == 2
    assert len(fresh.context["send_back_feedback"]) == 2


async def test_ci_rounds_cap_escalates_with_the_named_check(store):
    t = await _approval_task(store)
    w = _watcher(store, checks=[FAIL_CHECK])
    # Distinct signatures each round: vary the failing check name.
    for n in range(1, 4):
        w2 = _watcher(store, checks=[{**FAIL_CHECK, "name": f"check-{n}"}])
        t = await store.get_task(t.id)
        await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
        assert await w2._check_open_pr(t) == "resumed", f"round {n} should resume"
    w4 = _watcher(store, checks=[{**FAIL_CHECK, "name": "check-4"}])
    t = await store.get_task(t.id)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    out = await w4._check_open_pr(t)
    assert out == "escalated_ci"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED
    assert "check-4" in (fresh.blocker or {}).get("question", "")


async def test_pending_or_green_checks_do_nothing(store):
    t = await _approval_task(store)
    for checks in ([], [{**FAIL_CHECK, "status": "pending"}],
                   [{**FAIL_CHECK, "status": "pass"}]):
        assert await _watcher(store, checks=checks)._check_open_pr(t) is None
    assert (await store.get_task(t.id)).status is TaskStatus.AWAITING_APPROVAL


async def test_unknown_state_never_closes_or_completes(store):
    """gh missing / network down ⇒ state "" — must fall through, not act."""
    t = await _approval_task(store)
    out = await _watcher(store, state="")._check_open_pr(t)
    assert out is None
    assert (await store.get_task(t.id)).status is TaskStatus.AWAITING_APPROVAL
