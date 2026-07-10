"""Review continuity: the reviewer may not contradict prior rounds unknowingly.

Live oscillation on 84251cb2: round 14 "self-check is manual-only, enforce it"
→ round 15 "self-check errors every build, gate it" → rounds 16/17 "self-check
is out of scope, remove it". Fresh context each round, no memory of what it
had itself demanded.
"""

from __future__ import annotations

import pytest

from no_human.core.task import Task
from no_human.review.reviewer import ChecklistItem, ReviewDecision, _build_review_prompt


def _decision(passed, blocking=(), advisory=()):
    items = [ChecklistItem(lbl, False, f"{lbl} evidence", severity="high")
             for lbl in blocking]
    items += [ChecklistItem(lbl, False, f"{lbl} evidence", severity="nit")
              for lbl in advisory]
    return ReviewDecision(passed=passed, checklist=items, raw_output="")


def _orch(store=None):
    from no_human.core.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o._sink = lambda e: None
    if store is not None:
        o.store = store
    return o


# ── prompt side ───────────────────────────────────────────────────────────── #

def test_prompt_carries_continuity_and_the_no_relitigate_rule():
    t = Task.new("x")
    prompt = _build_review_prompt(
        t, "diff", "tests", "",
        prior_rounds="  - round 1 [FAIL]: parser drift — evidence",
    )
    assert "REVIEW CONTINUITY" in prompt
    assert "round 1 [FAIL]: parser drift" in prompt
    assert "do NOT reverse a prior round's request" in prompt.replace("Do NOT", "do NOT")
    assert "Operator answers above are binding" in prompt


def test_prompt_has_no_continuity_section_on_round_one():
    t = Task.new("x")
    prompt = _build_review_prompt(t, "diff", "tests", "")
    assert "REVIEW CONTINUITY" not in prompt


# ── orchestrator side ─────────────────────────────────────────────────────── #

@pytest.fixture
async def store(tmp_path):
    from no_human.core.db import Store
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


async def test_history_accumulates_and_feeds_the_next_round(store):
    t = Task.new("ci_gate", repo_path="/tmp/x")
    await store.create_task(t)
    o = _orch(store)

    await o._append_review_history(t, _decision(False, blocking=["image pinning"]))
    await o._append_review_history(
        t, _decision(False, blocking=["comment pagination"], advisory=["naming"]))

    text = o._review_continuity(t)
    assert "round 1 [FAIL]: image pinning" in text
    assert "round 2 [FAIL]: comment pagination" in text
    assert "(advisory: naming)" in text


async def test_operator_answers_are_injected_as_binding(store):
    t = Task.new("ci_gate", repo_path="/tmp/x")
    t.context = {"human_replies": [
        {"question": "q", "answer": "keep the self-check stage; it is in scope"},
    ]}
    await store.create_task(t)

    text = _orch(store)._review_continuity(t)
    assert "Operator answers (binding" in text
    assert "keep the self-check stage" in text


async def test_history_is_capped(store):
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    o = _orch(store)
    for i in range(20):
        await o._append_review_history(t, _decision(False, blocking=[f"f{i}"]))

    stored = t.context["review_history"]
    assert len(stored) == o._REVIEW_HISTORY_ROUNDS * 2
    # The continuity block shows only the most recent rounds.
    text = o._review_continuity(t)
    assert "f19" in text and "f0 " not in text


async def test_round_one_continuity_is_empty(store):
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    assert _orch(store)._review_continuity(t) == ""
