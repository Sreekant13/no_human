"""The awaiting-approval ladder: merged → done, closed → escalate, red CI →
bounded fix loop. Born from PR #7004: the Jenkinsfile died in Jenkins' CPS
compiler (MethodTooLargeException) while every local check passed, and nothing
watched the PR's own pipeline."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from no_human.blockers.wake import WakeWatcher
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


async def _approval_task(store, url="https://code.example.com/dev/x/pull/7004"):
    t = Task.new("ci_gate", repo_path="/tmp/x")
    t.context = {"pr_watch": url, "pr_branch": "scratch/x"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    return t


def _watcher(store, *, state="OPEN", checks=None, log="", events=None, comments=None,
             ignore_authors=None):
    async def pr_state(url): return state
    async def pr_checks(url): return checks or []
    async def ci_log(link): return log
    async def pr_comment(url): return comments or []
    # The ignore list is CONFIGURED here rather than inherited from a default.
    # It used to ship naming a real CI service account, which meant a test
    # about ignoring bot chatter only passed because the product came
    # pre-loaded with one employer's bot. A user sets their own; so does this.
    cfg = {"blockers": {"ignore_comment_authors": ignore_authors}} if ignore_authors else {}
    return WakeWatcher(
        store, cfg, pr_state=pr_state, pr_checks=pr_checks, ci_log=ci_log,
        pr_comment=pr_comment,
        on_event=(lambda k, t: events.append((k, t))) if events is not None else None,
    )


FAIL_CHECK = {
    "name": "continuous-integration/jenkins/pr-head", "status": "fail",
    "link": "https://build.example.com/.../PR-7004/2/display/redirect",
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


# ---- the self-comment loop (2026-07-10 P0 incident) ------------------------- #
# no_human posts its CI_GATE results comment under the operator's own gh login,
# so an author check can't distinguish the product's output from real feedback.
# The comment carries AGENT_COMMENT_MARKER; _is_self_or_bot filters it. Without
# these tests, the exact bug that flipped a green, merge-ready task to escalated
# (BUDGET_EXHAUSTED) had no regression pin.

async def test_own_agent_comment_never_resumes_the_task(store):
    from no_human.vcs.pr_watcher import AGENT_COMMENT_MARKER, PrComment
    t = await _approval_task(store)
    own = PrComment(
        author="dev",  # the operator's OWN login — an author check is blind
        body=f"{AGENT_COMMENT_MARKER}\n🧪 **CI_GATE Integration Test Results** ✅ passed",
        created_at="2026-07-10T19:05:41Z",
    )
    events = []
    out = await _watcher(store, comments=[own], events=events)._check_approval_pr_comments(t)
    assert out is None, "the product's own comment must not resume the task"
    assert (await store.get_task(t.id)).status is TaskStatus.AWAITING_APPROVAL
    assert not any(k == "resumed" for k, _ in events)
    assert any(k == "pr_feedback_skipped" for k, _ in events)


async def test_genuine_operator_comment_resumes_to_revise(store):
    """A real operator comment (no agent marker) IS feedback → resume."""
    from no_human.vcs.pr_watcher import PrComment
    t = await _approval_task(store)
    human = PrComment(author="dev", body="please rename the helper",
                      created_at="2026-07-10T20:00:00Z")
    events = []
    out = await _watcher(store, comments=[human], events=events)._check_approval_pr_comments(t)
    assert out == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    assert fresh.context["send_back_feedback"][-1]["message"] == "please rename the helper"
    assert any(k == "resumed" for k, _ in events)


async def test_self_comment_advances_the_cursor(store):
    """After skipping its own comment the cursor moves past it, so a later tick
    never reconsiders it (the incident re-fired every heartbeat before this)."""
    from no_human.vcs.pr_watcher import AGENT_COMMENT_MARKER, PrComment
    t = await _approval_task(store)
    own = PrComment(author="dev",
                    body=f"{AGENT_COMMENT_MARKER}\nresults",
                    created_at="2026-07-10T19:05:41Z")
    await _watcher(store, comments=[own])._check_approval_pr_comments(t)
    fresh = await store.get_task(t.id)
    assert (fresh.context or {}).get("pr_comment_since") == "2026-07-10T19:05:41Z"


# ---- the stuck-active watchdog --------------------------------------------- #
# A task can hang mid-attempt (a wedged Agent-SDK session emits nothing).
# Without this, it sits IMPLEMENTING forever, invisible — the exact shape that
# needed a hand-run `nh doctor` to spot. The watchdog escalates it honestly.

def _stuck_watcher(store, *, minutes=30):
    return WakeWatcher(store, {"blockers": {"stuck_active_minutes": minutes}})


async def _active_task(store, *, last_event_age_min: float | None):
    t = Task.new("wedged", repo_path="/tmp/x")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
    if last_event_age_min is not None:
        await store.save_events(t.id, [{
            "source": "orchestrator", "kind": "attempt_start", "text": "",
            "ts": time.time() - last_event_age_min * 60,
        }])
    return t


async def test_a_stalled_active_task_escalates(store):
    t = await _active_task(store, last_event_age_min=60)
    now = datetime.now(timezone.utc)
    escalated = await _stuck_watcher(store, minutes=30)._escalate_if_stalled(t, now=now)
    assert escalated is True
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED
    assert (fresh.blocker or {}).get("category") == "NOVEL_UNKNOWN"
    assert "stalled" in (fresh.blocker or {}).get("question", "")


async def test_recent_activity_is_not_stalled(store):
    t = await _active_task(store, last_event_age_min=5)
    escalated = await _stuck_watcher(store, minutes=30)._escalate_if_stalled(
        t, now=datetime.now(timezone.utc))
    assert escalated is False
    assert (await store.get_task(t.id)).status is TaskStatus.IMPLEMENTING


async def test_a_task_that_never_emitted_is_left_to_the_normal_loop(store):
    t = await _active_task(store, last_event_age_min=None)
    escalated = await _stuck_watcher(store, minutes=30)._escalate_if_stalled(
        t, now=datetime.now(timezone.utc))
    assert escalated is False


async def test_the_watchdog_is_disabled_at_zero(store):
    t = await _active_task(store, last_event_age_min=600)  # 10h stale
    escalated = await _stuck_watcher(store, minutes=0)._escalate_if_stalled(
        t, now=datetime.now(timezone.utc))
    assert escalated is False, "stuck_active_minutes<=0 disables the watchdog"


async def test_a_pause_in_flight_is_not_overridden(store):
    """cancel_requested means a pause is already landing — don't race it."""
    t = await _active_task(store, last_event_age_min=120)
    t.cancel_requested = True
    escalated = await _stuck_watcher(store, minutes=30)._escalate_if_stalled(
        t, now=datetime.now(timezone.utc))
    assert escalated is False


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
    """A CI service account's per-build test-results table was injected as
    operator feedback and resumed the task straight into the budget gate —
    one wasted attempt per PR. Bot chatter must advance the cursor (never
    reconsidered) without resuming.

    The account name and the build's own numbers are deliberately generic: a
    real one names an employer's CI estate, and a test about ignoring bot
    chatter needs the SHAPE of a bot comment, not a transcript of one."""
    t = await _approval_task(store)
    t.context["pr_comment_since"] = "2026-07-10T00:00:00Z"
    await store.update_task(t)
    bots = [
        _Comment("ci-results-bot", "## Unit Test Results\n42 passed", "2026-07-10T11:15:52Z"),
        _Comment("renovate[bot]", "dep dashboard", "2026-07-10T11:16:00Z"),
    ]
    events = []
    out = await _watcher(store, comments=bots, events=events, ignore_authors=["ci-results-bot"])._check_open_pr(t)
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
        _Comment("ci-results-bot", "## Unit Test Results", "2026-07-10T11:15:52Z"),
        _Comment("dev", "please rename the stage", "2026-07-10T11:20:00Z"),
    ]
    out = await _watcher(store, comments=mixed,
                         ignore_authors=["ci-results-bot"])._check_open_pr(t)
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


async def test_the_products_own_comment_never_resumes_its_task(store):
    """The 2026-07-10 incident: the CI_GATE results comment — posted under the
    OPERATOR's gh login — was injected as human feedback and resumed the
    parked task straight into the budget gate. Author identity cannot catch
    this; the body marker must."""
    from no_human.vcs.pr_watcher import AGENT_COMMENT_MARKER
    t = await _approval_task(store)
    t.context["pr_comment_since"] = "2026-07-10T00:00:00Z"
    await store.update_task(t)
    own = _Comment(
        "dev",  # the operator's own login — NOT a bot author
        f"{AGENT_COMMENT_MARKER}\n🧪 **CI_GATE Integration Test Results** "
        "(no_human)\n\n✅ **SUCCESS** — pipeline 7008",
        "2026-07-10T18:56:51Z",
    )
    events = []
    out = await _watcher(store, comments=[own], events=events)._check_open_pr(t)
    assert out is None
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert not fresh.context.get("send_back_feedback")
    # Cursor advanced so the same comment is never reconsidered.
    assert fresh.context["pr_comment_since"] == "2026-07-10T18:56:51Z"
    assert any(k == "pr_feedback_skipped" for k, _ in events)


async def test_a_real_operator_comment_still_resumes(store):
    """The marker filter must not eat genuine feedback — even feedback that
    QUOTES agent output (the rendered comment never carries the invisible
    marker)."""
    t = await _approval_task(store)
    t.context["pr_comment_since"] = "2026-07-10T00:00:00Z"
    await store.update_task(t)
    real = _Comment(
        "dev",
        "The CI_GATE results look good but please rename the namespace",
        "2026-07-10T19:00:00Z",
    )
    out = await _watcher(store, comments=[real])._check_open_pr(t)
    assert out == "resumed"
    fresh = await store.get_task(t.id)
    assert "rename the namespace" in fresh.context["send_back_feedback"][0]["message"]


async def test_stalled_active_task_escalates_honestly(store):
    """2026-07-11: a hung Agent-SDK reviewer left a task in 'reviewing'
    forever, holding a worker slot and never failing. The watchdog escalates
    a task with no event past the threshold."""
    import time
    t = Task.new("stalled", repo_path="/tmp/x")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.REVIEWING, validate=False)
    # An event 50 minutes old (> the 40-min default threshold).
    await store.save_events(t.id, [{"source": "orchestrator", "kind": "state",
                                    "text": "reviewing", "ts": time.time() - 3000}])
    events = []
    w = _watcher(store, events=events)
    # The sweep judges only worker-claimed tasks — a hung session HOLDS a slot.
    actions = await w.tick(active_ids={t.id})
    assert (t.id, "escalated_stalled") in actions
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED
    assert "hung Agent-SDK" in (fresh.blocker or {}).get("root_cause_hypothesis", "")
    assert "stalled" in (fresh.blocker or {}).get("question", "")
    assert any(k == "escalated_stalled" for k, _ in events)


async def test_recently_active_task_is_not_escalated(store):
    """A task mid-run (recent events, or a long test <40m) must NOT trip the
    watchdog — no false positives on legitimately slow work."""
    import time
    t = Task.new("working", repo_path="/tmp/x")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.TESTING, validate=False)
    await store.save_events(t.id, [{"source": "agent", "kind": "tool_use",
                                    "text": "running tests", "ts": time.time() - 600}])
    actions = await _watcher(store).tick(active_ids={t.id})
    assert (t.id, "escalated_stalled") not in actions
    assert (await store.get_task(t.id)).status is TaskStatus.TESTING
