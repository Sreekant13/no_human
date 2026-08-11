"""A review gate that never RAN is an infra incident, not a human decision.

Observed live twice on 2026-08-11 — tasks ``ad5cde99`` and ``7d63dbe1`` both sat
in ``escalated`` for hours, waiting for a person, over a quota outage. The
blocker they carried:

    category: NOVEL_UNKNOWN, transient: false, wake_condition: null
    "the reviewer reached no verdict after 2 rounds (reviewer session error
     (error)). The review gate did not run, so this diff is unreviewed."

Every word of the prose is true and the ROUTING is wrong: the reviewer's Agent
SDK session errored, which is the same class of failure as the dead transport
one method above already parks. Nothing about the diff was judged, so there is
nothing for a human to decide — the honest move is to park with a
machine-checkable wake condition and run the gate again.

What must NOT change, and is pinned here as the control: a reviewer that RAN and
could not reach a verdict on the CONTENT (turn-starved, no parseable
REVIEW_JSON, no reviewer configured) still escalates to a person, and the diff
never passes unreviewed either way.
"""

from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from no_human.agent.claude_backend import AgentResult
from no_human.blockers.taxonomy import BlockerCategory
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.learning.queue import NON_LEARNABLE_CATEGORIES
from no_human.review.reviewer import AdversarialReviewer, ReviewerUnavailable

# The CLI's own wording for the wall these two tasks hit, and a generic session
# death that is NOT a transport failure (no "stream closed" / "connection
# error"), so the two branches are exercised by the shapes that actually occur.
_QUOTA_TEXT = (
    "You've hit your weekly limit. Your limit will reset at 3pm (Europe/Berlin)."
)
_GENERIC_ERROR_TEXT = (
    "Error: the agent session ended abnormally\n"
    "Traceback (most recent call last):\n"
    "  File \"cli.py\", line 1, in <module>\n"
    "RuntimeError: subprocess exited with code 1\n"
)


class _ErroringBackend:
    """A reviewer backend whose SESSION dies — the SDK never raises, it hands
    the failure back as a normal result with ``is_error`` set."""

    def __init__(self, text: str, stop_reason: str = "error"):
        self.text = text
        self.stop_reason = stop_reason
        self.rounds = 0

    async def run(self, prompt, **kwargs):
        self.rounds += 1
        return AgentResult(
            final_text=self.text, num_turns=0, is_error=True, tokens_used=7,
            session_id=None, stop_reason=self.stop_reason)


async def _real_reason(text: str, tmp_path, *, stop_reason: str = "error") -> str:
    """The escalation detail THE REAL REVIEWER produces for a dead session.

    Hand-spelling the string here is the blind spot this avoids: the consumer
    would then be tested against an input the producer never emits (exactly the
    coupling `test_the_transport_marker_survives_the_reviewers_tail_window`
    exists for, one file over).
    """
    backend = _ErroringBackend(text, stop_reason)
    reviewer = AdversarialReviewer(backend=backend)
    with pytest.raises(ReviewerUnavailable) as exc:
        await reviewer._agent_review("prompt", tmp_path)
    assert backend.rounds == 2, "the bounded infra retry still runs first"
    return str(exc.value)


# --------------------------------------------------------------------------- #
# harness — the real store, the real `_raise_blocker`, the real watcher.        #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def store(tmp_path):
    from no_human.core.db import Store

    s = await Store(tmp_path / "infra_park.db").connect()
    yield s
    await s.close()


def _orch_with(store, notes):
    class _Notifier:
        def notify(self, kind, line):
            notes.append((kind, line))

    orch = Orchestrator.__new__(Orchestrator)
    orch.store = store
    orch.notifier = _Notifier()
    orch.emit = lambda *a, **k: None
    orch.config = {}
    orch.learning_queue = None
    return orch


async def _new_task(store) -> Task:
    task = Task.new("do a thing", repo_path="/tmp/x")
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    return task


# --------------------------------------------------------------------------- #
# (a) the quota family                                                         #
# --------------------------------------------------------------------------- #


async def test_a_quota_killed_review_gate_parks_on_quota_and_never_escalates(
        store, tmp_path):
    """The exact incident. A quota wall took the reviewer's session down; the
    diff is unreviewed, not rejected, and a person has nothing to decide."""
    notes: list = []
    orch = _orch_with(store, notes)
    task = await _new_task(store)
    detail = await _real_reason(_QUOTA_TEXT, tmp_path)

    outcome = await orch._escalate_reviewer_unavailable(task, detail)
    parked = await store.get_task(task.id)

    assert outcome.status == TaskStatus.PAUSED_QUOTA, (
        "a billing wall burned a human escalation instead of parking")
    assert parked.blocker["category"] == BlockerCategory.QUOTA.value
    assert parked.blocker["transient"] is True
    assert parked.blocker["wake_condition"] == "quota_refreshed"
    assert parked.wake_check_at, "a quota park with no re-check stamp never wakes"
    assert parked.blocker["category"] in NON_LEARNABLE_CATEGORIES, (
        "an outage was proposed to the human as a durable code lesson")
    # The evidence a human reads still says the gate did not run.
    assert "unreviewed" in parked.blocker["evidence"]


# --------------------------------------------------------------------------- #
# (b) any other dead reviewer session                                          #
# --------------------------------------------------------------------------- #


async def test_a_generic_dead_reviewer_session_parks_transient_with_a_timer(
        store, tmp_path):
    from no_human.blockers.wake import WakeWatcher

    notes: list = []
    orch = _orch_with(store, notes)
    task = await _new_task(store)
    detail = await _real_reason(_GENERIC_ERROR_TEXT, tmp_path)

    outcome = await orch._escalate_reviewer_unavailable(task, detail)
    parked = await store.get_task(task.id)

    assert outcome.status == TaskStatus.BLOCKED
    assert parked.blocker["category"] == BlockerCategory.TRANSIENT_INFRA.value
    assert parked.blocker["transient"] is True
    assert parked.blocker["wake_condition"] == "after:30m"
    # ...and the park really does self-fire, through the real watcher.
    cfg = {"blockers": {"max_park_duration": "48h"}}
    due = datetime.fromisoformat(parked.wake_check_at) + timedelta(minutes=1)
    assert (task.id, "resumed") in await WakeWatcher(store, cfg).tick(now=due)
    # The human still hears about an unreviewed diff the same minute (the same
    # `notify_override` reasoning the transport sibling documents).
    assert notes, "a dead review gate parked SILENTLY for max_park (48h)"


# --------------------------------------------------------------------------- #
# (c) THE CONTROL — a reviewer that RAN still reaches a human                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("detail", [
    # the reviewer explored, spoke, and produced nothing parseable
    "the reviewer reached no verdict after 2 rounds (no REVIEW_JSON block)",
    # turn-starved twice: it RAN, it was cut off mid-exploration
    "the reviewer reached no verdict after 2 rounds "
    "(reviewer session error (max_turns))",
    # an operator problem, not an outage
    "no reviewer is configured, so the review gate cannot run.",
])
async def test_a_reviewer_that_ran_still_escalates_to_a_human(store, detail):
    """Without this control the fix above passes for a function that parks
    EVERY unavailable reviewer — turning a genuinely stuck gate, and a
    misconfigured one, into an invisible retry loop."""
    notes: list = []
    orch = _orch_with(store, notes)
    task = await _new_task(store)

    outcome = await orch._escalate_reviewer_unavailable(task, detail)
    escalated = await store.get_task(task.id)

    assert outcome.status == TaskStatus.ESCALATED
    assert escalated.blocker["category"] == BlockerCategory.NOVEL_UNKNOWN.value
    assert escalated.blocker["transient"] is False
    assert not escalated.blocker.get("wake_condition")
    assert escalated.wake_check_at is None


async def test_two_truncated_rounds_are_not_a_dead_session(tmp_path):
    """The producer half of the control: a reviewer cut off at its turn budget
    must NOT be marked as a dead session, or the routing above cannot tell
    "turn-starved" from "the session died"."""
    from no_human.review.reviewer import REVIEW_SESSION_ERROR_MARKER

    detail = await _real_reason("partial exploration, no verdict yet",
                                tmp_path, stop_reason="max_turns")
    assert "reviewer session error (max_turns)" in detail
    assert REVIEW_SESSION_ERROR_MARKER not in detail


def test_the_marker_the_orchestrator_matches_is_the_one_the_reviewer_writes():
    """One constant, imported by both ends. A literal repeated in two files is
    the shape that silently stops matching (the transport marker's own lesson)."""
    from no_human.core import orchestrator
    from no_human.review import reviewer

    assert (orchestrator._SESSION_ERROR_BLOCKER_MARKER
            is reviewer.REVIEW_SESSION_ERROR_MARKER)


# --------------------------------------------------------------------------- #
# (d) the cap — an infinite park loop is worse than an escalation               #
# --------------------------------------------------------------------------- #


async def test_the_fourth_consecutive_infra_park_escalates_for_real(
        store, tmp_path):
    from no_human.core.orchestrator import _MAX_REVIEW_INFRA_PARKS

    assert _MAX_REVIEW_INFRA_PARKS == 3
    notes: list = []
    orch = _orch_with(store, notes)
    task = await _new_task(store)
    detail = await _real_reason(_GENERIC_ERROR_TEXT, tmp_path)

    for n in range(_MAX_REVIEW_INFRA_PARKS):
        outcome = await orch._escalate_reviewer_unavailable(task, detail)
        assert outcome.status == TaskStatus.BLOCKED, (
            f"park {n + 1} of {_MAX_REVIEW_INFRA_PARKS} should still self-heal")

    outcome = await orch._escalate_reviewer_unavailable(task, detail)
    final = await store.get_task(task.id)

    assert outcome.status == TaskStatus.ESCALATED, (
        "the gate has now failed to run 4 times — parking again is a loop")
    assert final.wake_check_at is None
    assert not final.blocker["wake_condition"], (
        "an escalated task the watcher never sweeps still advertises a retry")
    # The honest text survives: the human is told the diff is UNREVIEWED, and
    # that this was infrastructure rather than a finding about the change.
    report = f"{final.blocker['root_cause_hypothesis']} {final.blocker['evidence']}"
    assert "unreviewed" in report.lower()
    assert final.blocker["category"] in NON_LEARNABLE_CATEGORIES, (
        "four outages are still an outage, not a lesson about the repo")


async def test_a_quota_park_and_an_infra_park_share_one_budget(store, tmp_path):
    """The cap counts REVIEW PARKS, not one counter per category — alternating
    failures must not reset each other into an unbounded loop."""
    from no_human.core.orchestrator import _MAX_REVIEW_INFRA_PARKS

    orch = _orch_with(store, [])
    task = await _new_task(store)
    quota = await _real_reason(_QUOTA_TEXT, tmp_path)
    generic = await _real_reason(_GENERIC_ERROR_TEXT, tmp_path)

    details = [quota, generic, quota, generic][:_MAX_REVIEW_INFRA_PARKS + 1]
    statuses = [(await orch._escalate_reviewer_unavailable(task, d)).status
                for d in details]

    assert statuses[-1] == TaskStatus.ESCALATED
    assert TaskStatus.ESCALATED not in statuses[:-1]


# --------------------------------------------------------------------------- #
# (e) the wake resumes the REVIEW, from the coder's finished work               #
# --------------------------------------------------------------------------- #


async def test_the_wake_resumes_from_the_checkpoint_not_from_base(
        store, tmp_path):
    """The coder's work is COMPLETE when the gate dies — the resume must
    continue from the checkpoint the blocker recorded, or the next attempt
    branches from base and redoes everything the parked attempt committed."""
    from no_human.blockers.wake import WakeWatcher

    sha = "a" * 40

    class _Repo:
        path = tmp_path

        def has_changes(self):
            return False

        def head_sha(self):
            return sha

    orch = _orch_with(store, [])
    task = await _new_task(store)
    detail = await _real_reason(_GENERIC_ERROR_TEXT, tmp_path)

    await orch._escalate_reviewer_unavailable(
        task, detail, repo=_Repo(), branch="nh/t1-review")
    parked = await store.get_task(task.id)

    assert parked.blocker["resume_commit"] == sha
    assert parked.blocker["resume_branch"] == "nh/t1-review"

    due = datetime.fromisoformat(parked.wake_check_at) + timedelta(minutes=1)
    actions = await WakeWatcher(
        store, {"blockers": {"max_park_duration": "48h"}}).tick(now=due)
    assert (task.id, "resumed") in actions

    resumed = await store.get_task(task.id)
    assert resumed.context["resume_from"]["sha"] == sha, (
        "the resume threw away the reviewed-but-never-judged commit")
    assert resumed.context["resume_from"]["by"] == "wake"
