"""Bugfix: a human's PR review comment was skipped for the whole tick whenever
the conflict rung acted first (task 1e5583dc / PR #593, measured 2026-08-21) —
the comment rung is the only place that advances `pr_comment_since` and
injects human PR comments into `send_back_feedback`, but the old rung ladder
ran the conflict rung first with an early return, so the comment rung never
ran that tick. This file pins the chosen fix: the comment rung always injects
FIRST in inject-only mode, then whichever rung ends the tick carries both
payloads in one resume; a deferral (when nothing consumes the injected
findings this tick) emits a named `pr_feedback_deferred` event."""

from __future__ import annotations

import inspect

import pytest

from no_human.blockers.wake import WakeWatcher
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.vcs import derived_conflict as dc
from no_human.vcs.pr_watcher import PrComment


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


@pytest.fixture(autouse=True)
def _resolvable_conflicting_paths(monkeypatch):
    """Same stub as `test_wake_conflict.py`: this file drives the conflict rung
    against a fake, non-existent `repo_path` ("/tmp/x"), so real conflicting-
    path enumeration must be stubbed to a fixed, non-derived path or every test
    here would exercise the enumeration-failure branch instead of the "real
    source conflict" branch its assertions are written against."""
    async def fake_conflicting_paths(repo_path, base_tip, branch):
        return {"src/unrelated.py"}
    monkeypatch.setattr(dc, "conflicting_paths", fake_conflicting_paths)


async def _approval_task(store, url="https://code.example.com/dev/x/pull/593"):
    t = Task.new("conflict", repo_path="/tmp/x")
    t.context = {"pr_watch": url, "pr_branch": "scratch/x", "base_branch": "main"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    return t


def _watcher(store, *, mergeable_sequence=None, mergeable=None, merge_state="",
             comments=None, events=None):
    """Composed from `test_wake_conflict.py::_watcher` (the `pr_mergeable`
    wiring) and `test_pr_ci_watch.py::_watcher` (the `pr_comment` wiring) —
    this is the only rung pair this bugfix concerns, so no other checker is
    wired."""
    seq = list(mergeable_sequence) if mergeable_sequence is not None else None

    async def pr_mergeable(url):
        nonlocal seq
        if seq is not None:
            value = seq.pop(0) if seq else (mergeable or "")
        else:
            value = mergeable or ""
        return {"mergeable": value, "mergeStateStatus": merge_state}

    async def pr_comment(url):
        return comments or []

    return WakeWatcher(
        store, {},
        pr_mergeable=pr_mergeable,
        pr_comment=pr_comment,
        on_event=(lambda k, t: events.append((k, t))) if events is not None else None,
    )


# --------------------------------------------------------------------------- #
# AC 1 — combined payload
# --------------------------------------------------------------------------- #

async def test_conflicting_pr_with_fresh_human_comment_resumes_with_both_payloads(store):
    t = await _approval_task(store)
    human = PrComment(author="dev", body="please rename the helper",
                       created_at="2026-08-21T20:47:10Z")
    w = _watcher(store, mergeable="CONFLICTING", merge_state="DIRTY",
                 comments=[human])
    out = await w._check_open_pr(t)
    assert out == "resumed"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.IMPLEMENTING
    fb = fresh.context["send_back_feedback"]
    assert len(fb) == 2, (
        "RED on current main: only the pr_conflict entry is present because "
        "the conflict rung acted first and returned before the comment rung "
        "ever ran"
    )
    sources = {e["source"] for e in fb}
    assert sources == {"pr_comment", "pr_conflict"}
    comment_entry = next(e for e in fb if e["source"] == "pr_comment")
    assert "please rename the helper" in comment_entry["message"]
    conflict_entry = next(e for e in fb if e["source"] == "pr_conflict")
    assert "rebase" in conflict_entry["message"].lower()


async def test_the_comment_cursor_advances_so_the_same_comment_is_not_reinjected(store):
    t = await _approval_task(store)
    human = PrComment(author="dev", body="please rename the helper",
                       created_at="2026-08-21T20:47:10Z")
    w = _watcher(store, mergeable_sequence=["CONFLICTING", "MERGEABLE"],
                 merge_state="DIRTY", comments=[human])

    assert await w._check_open_pr(t) == "resumed"
    t = await store.get_task(t.id)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    out = await w._check_open_pr(t)
    # The conflict is now MERGEABLE (resolved) and the same comment is still
    # returned by the fake `pr_comment` hook, but its `created_at` is no
    # longer newer than the advanced `pr_comment_since` cursor, so neither
    # rung has anything to act on this tick.
    assert out is None
    fresh = await store.get_task(t.id)
    fb = fresh.context["send_back_feedback"]
    comment_entries = [e for e in fb if e["source"] == "pr_comment"]
    assert len(comment_entries) == 1, "the same comment must not be reinjected"
    assert fresh.context.get("pr_comment_since") == "2026-08-21T20:47:10Z"


# --------------------------------------------------------------------------- #
# AC 2 — the chosen precedence is stated at the ordering site
# --------------------------------------------------------------------------- #

def test_the_rung_ordering_site_states_what_the_losing_rung_gives_up():
    src = inspect.getsource(WakeWatcher._check_open_pr)
    assert src.count("GIVES UP") >= 2, (
        "the ordering site must name what BOTH the comment rung and the "
        "conflict rung give up under the chosen precedence"
    )
    assert "conflict rung" in src


# --------------------------------------------------------------------------- #
# AC 3 — a deferral (if the chosen shape still defers anything) is named
# --------------------------------------------------------------------------- #

async def test_a_conflict_escalation_after_injection_emits_a_named_deferral(store):
    t = await _approval_task(store)
    events = []
    # Drive pr_conflict_rounds past max_pr_conflict_rounds (default 3) with no
    # comments in play yet -- these rounds must resume normally.
    w_warmup = _watcher(store, mergeable="CONFLICTING", merge_state="DIRTY")
    for n in range(1, 4):
        assert await w_warmup._check_open_pr(t) == "resumed", f"round {n} should resume"
        t = await store.get_task(t.id)
        await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    # A fresh human comment arrives on the round that will push the conflict
    # rung past its bound: the comment rung injects it (inject-only) THIS
    # tick, but the conflict rung ends the tick by escalating instead of
    # resuming, so the injected findings are not carried into any coder round.
    human = PrComment(author="dev", body="please rename the helper",
                       created_at="2026-08-21T20:47:10Z")
    w = _watcher(store, mergeable="CONFLICTING", merge_state="DIRTY",
                 comments=[human], events=events)
    out = await w._check_open_pr(t)
    assert out == "escalated_pr_conflict"
    fresh = await store.get_task(t.id)
    assert fresh.status is TaskStatus.ESCALATED

    fb = fresh.context.get("send_back_feedback") or []
    assert any(e["source"] == "pr_comment" for e in fb), (
        "the human's comment is still injected into send_back_feedback even "
        "though the task escalated instead of resuming"
    )

    assert any(k == "pr_feedback_deferred" for k, _ in events)
    persisted = await store.list_events(t.id)
    deferred = [e for e in persisted if e.get("kind") == "pr_feedback_deferred"]
    assert deferred, "the deferral must be persisted, not just callback-emitted"
    assert "escalated_pr_conflict" in deferred[-1]["text"], (
        "the message must name the outcome that consumed the tick instead of "
        "the human's findings"
    )


async def test_no_deferral_event_when_the_round_actually_started(store):
    t = await _approval_task(store)
    human = PrComment(author="dev", body="please rename the helper",
                       created_at="2026-08-21T20:47:10Z")
    events = []
    w = _watcher(store, mergeable="CONFLICTING", merge_state="DIRTY",
                 comments=[human], events=events)
    out = await w._check_open_pr(t)
    assert out == "resumed"
    assert not any(k == "pr_feedback_deferred" for k, _ in events), (
        "when the conflict rung's own resume carries both payloads, nothing "
        "was deferred and no deferral event should fire"
    )


def test_the_deferral_kind_is_labelled_on_the_board_and_counted_by_doctor():
    from pathlib import Path

    import no_human.doctor as doctor_mod

    slideover = Path(__file__).resolve().parents[1] / "web" / "src" / "SlideOver.jsx"
    text = slideover.read_text()
    assert "pr_feedback_deferred:" in text, (
        "the board must map the raw kind to a human-readable label instead "
        "of falling back to the raw kind string"
    )

    src = inspect.getsource(doctor_mod)
    assert "pr_watch_ladder" in src
    # The MECHANISMS table is a module-level structure; find the tuple that
    # names pr_watch_ladder's own event kinds and assert the new kind is in it
    # rather than re-deriving the whole table by hand.
    idx = src.index("pr_watch_ladder")
    window = src[idx: idx + 800]
    assert "pr_feedback_deferred" in window, (
        "nh doctor's pr_watch_ladder mechanism must count pr_feedback_deferred"
    )


# --------------------------------------------------------------------------- #
# AC 4 — existing rungs unchanged, bot-only comments still a no-op for the
# comment rung and never block the conflict rung
# --------------------------------------------------------------------------- #

async def test_bot_only_comments_do_not_block_the_conflict_rung(store):
    from no_human.vcs.pr_watcher import AGENT_COMMENT_MARKER

    t = await _approval_task(store)
    own = PrComment(
        author="dev",  # the operator's own login posts the product's own
        # comment, so an author check alone can't distinguish it -- see
        # test_pr_ci_watch.py's self-comment loop tests.
        body=f"{AGENT_COMMENT_MARKER}\nresults",
        created_at="2026-08-21T19:05:41Z",
    )
    events = []
    w = _watcher(store, mergeable="CONFLICTING", merge_state="DIRTY",
                 comments=[own], events=events)
    out = await w._check_open_pr(t)
    assert out == "resumed"
    fresh = await store.get_task(t.id)
    fb = fresh.context["send_back_feedback"]
    assert len(fb) == 1, "a bot/self comment must not be injected as feedback"
    assert fb[0]["source"] == "pr_conflict"
    assert not any(k == "pr_feedback_deferred" for k, _ in events), (
        "nothing was injected, so there is nothing to defer"
    )
