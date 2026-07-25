"""SCRUM-41: a PR that textually conflicts with main is invisible to CI (branch
checks only run the PR's own branch) — live occurrence: PR #26 conflicted with
#25 and sat invisible until a human tried to merge it. This rung polls
`gh pr view --json mergeable,mergeStateStatus` directly and bounds the
rebase→repoll cycle the same way the CI-fix rung bounds its own rounds."""

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


async def _approval_task(store, url="https://code.example.com/dev/x/pull/26"):
    t = Task.new("conflict", repo_path="/tmp/x")
    t.context = {"pr_watch": url, "pr_branch": "scratch/x"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    return t


def _watcher(store, *, mergeable_sequence=None, mergeable=None, merge_state="",
             events=None):
    """``mergeable_sequence`` (a list) is popped from the front on each call —
    lets a test script CONFLICTING → UNKNOWN → CONFLICTING across ticks.
    ``mergeable`` is a fixed single value used when no sequence is given."""
    seq = list(mergeable_sequence) if mergeable_sequence is not None else None

    async def pr_mergeable(url):
        nonlocal seq
        if seq is not None:
            value = seq.pop(0) if seq else (mergeable or "")
        else:
            value = mergeable or ""
        return {"mergeable": value, "mergeStateStatus": merge_state}

    return WakeWatcher(
        store, {},
        pr_mergeable=pr_mergeable,
        on_event=(lambda k, t: events.append((k, t))) if events is not None else None,
    )


async def test_conflicting_sends_back_a_rebase_instruction_and_resumes(store):
    t = await _approval_task(store)
    events = []
    w = _watcher(store, mergeable="CONFLICTING", merge_state="DIRTY", events=events)
    out = await w._check_open_pr(t)
    assert out == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    fb = fresh.context["send_back_feedback"]
    assert len(fb) == 1
    assert fb[-1]["source"] == "pr_conflict"
    assert "rebase" in fb[-1]["message"].lower()
    assert "conflict" in fb[-1]["message"].lower()
    assert fresh.context["pr_conflict_rounds"] == 1
    assert any(k == "pr_conflict" for k, _ in events)


async def test_unknown_mergeable_is_a_pure_noop(store):
    t = await _approval_task(store)
    events = []
    w = _watcher(store, mergeable="UNKNOWN", events=events)
    out = await w._check_open_pr(t)
    assert out is None
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert not fresh.context.get("send_back_feedback")
    assert "pr_conflict_rounds" not in (fresh.context or {})
    assert not any(k in ("pr_conflict", "escalated_pr_conflict") for k, _ in events)


async def test_empty_mergeable_poll_miss_is_a_pure_noop(store):
    """gh missing / network error ⇒ mergeable "" — must never act."""
    t = await _approval_task(store)
    w = _watcher(store, mergeable="")
    out = await w._check_open_pr(t)
    assert out is None
    assert (await store.get_task(t.id)).status is TaskStatus.AWAITING_APPROVAL


async def test_mergeable_state_is_a_pure_noop_when_never_conflicted(store):
    t = await _approval_task(store)
    w = _watcher(store, mergeable="MERGEABLE")
    out = await w._check_open_pr(t)
    assert out is None
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.AWAITING_APPROVAL
    assert "pr_conflict_rounds" not in (fresh.context or {})


async def test_no_pr_mergeable_hook_wired_is_a_noop(store):
    """The rung must be inert (not crash) when the host never wired the hook."""
    t = await _approval_task(store)
    w = WakeWatcher(store, {})
    out = await w._check_open_pr(t)
    assert out is None


async def test_a_repeat_conflicting_poll_is_a_second_round(store):
    t = await _approval_task(store)
    w = _watcher(store, mergeable="CONFLICTING")
    assert await w._check_open_pr(t) == "resumed"
    t = await store.get_task(t.id)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    assert await w._check_open_pr(t) == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.context["pr_conflict_rounds"] == 2
    assert len(fresh.context["send_back_feedback"]) == 2


async def test_bound_enforcement_escalates_naming_pr_and_merge_state(store):
    url = "https://code.example.com/dev/x/pull/26"
    t = await _approval_task(store, url=url)
    w = _watcher(store, mergeable="CONFLICTING", merge_state="DIRTY")
    for n in range(1, 4):
        t = await store.get_task(t.id)
        await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
        assert await w._check_open_pr(t) == "resumed", f"round {n} should resume"
    t = await store.get_task(t.id)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    out = await w._check_open_pr(t)
    assert out == "escalated_pr_conflict"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED
    question = (fresh.blocker or {}).get("question", "")
    assert url in question
    assert "DIRTY" in question
    assert fresh.context["pr_conflict_rounds"] == 4


async def test_interleaved_unknown_between_conflicts_does_not_reset_the_bound(store):
    """The exact gap the reviewer flagged: CONFLICTING resumes the coder, the
    coder rebases and pushes, the very next tick sees UNKNOWN while GitHub
    recomputes mergeability, then it settles back to CONFLICTING. The round
    counter must keep climbing through the UNKNOWN tick and still escalate at
    the bound — not reset on every rebase cycle and loop forever."""
    t = await _approval_task(store)
    w = _watcher(store, mergeable_sequence=[
        "CONFLICTING",  # round 1 -> resumed
        "UNKNOWN",      # GitHub recomputing after the rebase push -> no-op
        "CONFLICTING",  # round 2 -> resumed
        "UNKNOWN",      # recomputing again -> no-op
        "CONFLICTING",  # round 3 -> resumed
        "UNKNOWN",      # recomputing again -> no-op
        "CONFLICTING",  # round 4 -> past bound -> escalate
    ])

    async def _tick():
        nonlocal t
        t = await store.get_task(t.id)
        await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
        return await w._check_open_pr(t)

    assert await _tick() == "resumed"          # round 1
    assert await _tick() is None                # UNKNOWN: no-op
    fresh = await store.get_task(t.id)
    assert fresh.context["pr_conflict_rounds"] == 1, "UNKNOWN must not reset the counter"

    assert await _tick() == "resumed"           # round 2
    assert await _tick() is None                # UNKNOWN: no-op
    assert await _tick() == "resumed"           # round 3
    assert await _tick() is None                # UNKNOWN: no-op
    out = await _tick()                          # round 4: past bound
    assert out == "escalated_pr_conflict"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED
    assert fresh.context["pr_conflict_rounds"] == 4


async def test_mergeable_after_conflict_resets_the_round_counter(store):
    """A resolved conflict followed by a NEW conflict is a distinct failure
    cycle — the round counter starts over once GitHub confirms MERGEABLE."""
    t = await _approval_task(store)
    w = _watcher(store, mergeable_sequence=["CONFLICTING", "MERGEABLE", "CONFLICTING"])

    async def _tick():
        nonlocal t
        t = await store.get_task(t.id)
        await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
        return await w._check_open_pr(t)

    assert await _tick() == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.context["pr_conflict_rounds"] == 1

    assert await _tick() is None  # MERGEABLE: resolved, resets
    fresh = await store.get_task(t.id)
    assert fresh.context["pr_conflict_rounds"] == 0

    assert await _tick() == "resumed"  # a fresh conflict cycle
    fresh = await store.get_task(t.id)
    assert fresh.context["pr_conflict_rounds"] == 1


async def test_merged_pr_skips_the_conflict_rung_entirely(store):
    """A merged PR must go straight to DONE without even polling mergeability."""
    t = await _approval_task(store)
    polled = []

    async def pr_state(url):
        return "MERGED"

    async def pr_mergeable(url):
        polled.append(url)
        return {"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}

    w = WakeWatcher(store, {}, pr_state=pr_state, pr_mergeable=pr_mergeable)
    out = await w._check_open_pr(t)
    assert out == "merged"
    assert not polled, "a merged PR must never poll mergeability"
    assert (await store.get_task(t.id)).status is TaskStatus.DONE


async def test_closed_pr_skips_the_conflict_rung_entirely(store):
    t = await _approval_task(store)
    polled = []

    async def pr_state(url):
        return "CLOSED"

    async def pr_mergeable(url):
        polled.append(url)
        return {"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}

    w = WakeWatcher(store, {}, pr_state=pr_state, pr_mergeable=pr_mergeable)
    out = await w._check_open_pr(t)
    assert out == "escalated_pr_closed"
    assert not polled, "a closed PR must never poll mergeability"
    assert (await store.get_task(t.id)).status is TaskStatus.ESCALATED


async def test_task_without_a_pr_link_is_left_untouched(store):
    t = Task.new("no-pr", repo_path="/tmp/x")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    polled = []

    async def pr_mergeable(url):
        polled.append(url)
        return {"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}

    w = WakeWatcher(store, {}, pr_mergeable=pr_mergeable)
    out = await w._check_open_pr(t)
    assert out is None
    assert not polled
    assert (await store.get_task(t.id)).status is TaskStatus.AWAITING_APPROVAL


async def test_gh_poll_error_is_a_noop_not_a_crash(store):
    t = await _approval_task(store)

    async def pr_mergeable(url):
        raise RuntimeError("network error")

    w = WakeWatcher(store, {}, pr_mergeable=pr_mergeable)
    out = await w._check_open_pr(t)
    assert out is None
    assert (await store.get_task(t.id)).status is TaskStatus.AWAITING_APPROVAL
