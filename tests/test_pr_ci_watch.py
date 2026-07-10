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


def _watcher(store, *, state="OPEN", checks=None, log="", events=None, comments=None):
    async def pr_state(url): return state
    async def pr_checks(url): return checks or []
    async def ci_log(link): return log
    async def pr_comment(url): return comments or []
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


class _Comment:
    def __init__(self, author, body, created_at):
        self.author = author
        self.body = body
        self.created_at = created_at
        self.path = self.line = self.diff_hunk = None


async def test_bot_comments_never_trigger_a_revision(store):
    """Live incident: system-codeadmin's per-build test-results table was
    injected as operator feedback and resumed the task straight into the
    budget gate — one wasted attempt per PR. Bot chatter must advance the
    cursor (never reconsidered) without resuming."""
    t = await _approval_task(store)
    t.context["pr_comment_since"] = "2026-07-10T00:00:00Z"
    await store.update_task(t)
    bots = [
        _Comment("system-codeadmin", "## Unit Test Results\n583 passed", "2026-07-10T11:15:52Z"),
        _Comment("renovate[bot]", "dep dashboard", "2026-07-10T11:16:00Z"),
    ]
    events = []
    out = await _watcher(store, comments=bots, events=events)._check_open_pr(t)
    assert out is None
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert not fresh.context.get("send_back_feedback")
    assert fresh.context["pr_comment_since"] == "2026-07-10T11:16:00Z"
    assert any(k == "pr_feedback_skipped" for k, _ in events)
    # The skip is persisted as a task event even with no host callback wired —
    # the server ran with a completely silent watcher for exactly this reason.
    persisted = await store.list_events(t.id)
    assert any(e["kind"] == "pr_feedback_skipped" and e["source"] == "watcher"
               for e in persisted)


async def test_human_comment_mixed_with_bot_chatter_injects_only_the_human(store):
    t = await _approval_task(store)
    t.context["pr_comment_since"] = "2026-07-10T00:00:00Z"
    await store.update_task(t)
    mixed = [
        _Comment("system-codeadmin", "## Unit Test Results", "2026-07-10T11:15:52Z"),
        _Comment("dev", "please rename the stage", "2026-07-10T11:20:00Z"),
    ]
    out = await _watcher(store, comments=mixed)._check_open_pr(t)
    assert out == "resumed"
    fresh = await store.get_task(t.id)
    fb = fresh.context["send_back_feedback"]
    assert len(fb) == 1
    assert "rename the stage" in fb[0]["message"]


async def test_pending_or_green_checks_do_nothing(store):
    t = await _approval_task(store)
    for checks in ([], [{**FAIL_CHECK, "status": "pending"}],
                   [{**FAIL_CHECK, "status": "pass"}]):
        assert await _watcher(store, checks=checks)._check_open_pr(t) is None
    assert (await store.get_task(t.id)).status is TaskStatus.AWAITING_APPROVAL


async def test_an_idle_tick_leaves_a_throttled_heartbeat(store):
    """A healthy parked task produces no action events, which used to be
    indistinguishable from a dead watcher. The tick now persists one
    wake_tick per task per hour — proof of life without event spam."""
    t = await _approval_task(store)
    w = _watcher(store)  # green checks, no comments: nothing to do
    await w.tick()
    await w.tick()  # immediately again — must not duplicate
    evs = [e for e in await store.list_events(t.id) if e["kind"] == "wake_tick"]
    assert len(evs) == 1
    assert evs[0]["source"] == "watcher"
    assert (await store.get_task(t.id)).status is TaskStatus.AWAITING_APPROVAL


async def test_unknown_state_never_closes_or_completes(store):
    """gh missing / network down ⇒ state "" — must fall through, not act."""
    t = await _approval_task(store)
    out = await _watcher(store, state="")._check_open_pr(t)
    assert out is None
    assert (await store.get_task(t.id)).status is TaskStatus.AWAITING_APPROVAL
