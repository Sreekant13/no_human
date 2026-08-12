"""Memory lifecycle B: scored selection (learning/ranking.py).

Covers the acceptance criteria: the formula (with hand-computed worked
examples), strict score ordering under budget pressure, the hard 25-memory
ceiling, the top-5-importance:high-by-use_count floor, the empty-ledger
fallback to importance x recency, determinism, "no LLM in the hot path", and
that scoring only reorders the already trigger-filtered + term-screened set.
"""

from __future__ import annotations

import inspect
import math
import random
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from no_human.core.prompt_blocks import build_memories_block
from no_human.learning.ranking import (
    FLOOR_RANK,
    MEMORY_CEILING,
    memory_score,
    rank_and_select,
)

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)


def _mem(mem_id, *, tier="med", days_ago=0, use_count=None, title=None,
         content="x", tags_extra=None):
    tags = list(tags_extra or [])
    if tier == "high":
        tags.append("importance:high")
    elif tier == "low":
        tags.append("importance:low")
    m = {
        "id": mem_id,
        "type": "rule",
        "title": title or f"mem-{mem_id}",
        "content": content,
        "tags": tags,
        "last_used_at": (NOW - timedelta(days=days_ago)).isoformat(),
    }
    if use_count is not None:
        m["use_count"] = use_count
    return m


# --------------------------------------------------------------------------- #
# Formula
# --------------------------------------------------------------------------- #

def test_score_matches_hand_computed_formula():
    high = _mem("a", tier="high", days_ago=7, use_count=8)
    med = _mem("b", tier="med", days_ago=0, use_count=4)
    low = _mem("c", tier="low", days_ago=28, use_count=0)
    max_use_count = 8

    s_high = memory_score(high, now=NOW, max_use_count=max_use_count)
    s_med = memory_score(med, now=NOW, max_use_count=max_use_count)
    s_low = memory_score(low, now=NOW, max_use_count=max_use_count)

    assert s_high == pytest.approx(1.0 * math.exp(-7 / 14) * (9 / 9), abs=1e-9)
    assert s_med == pytest.approx(0.5 * math.exp(0 / 14) * (5 / 9), abs=1e-9)
    assert s_low == pytest.approx(0.2 * math.exp(-28 / 14) * (1 / 9), abs=1e-9)
    assert s_high > s_med > s_low


# --------------------------------------------------------------------------- #
# Ordering under budget pressure
# --------------------------------------------------------------------------- #

def test_ordering_is_strict_score_order_under_pressure():
    # Distinct use_counts (all zero -> no floor candidates) and distinct
    # last_used_at so every score is unique; med tier throughout so no
    # importance:high floor can intervene.
    memories = [
        _mem(str(i), tier="med", days_ago=i, use_count=0)
        for i in range(40)
    ]
    random.Random(7).shuffle(memories)

    selected = rank_and_select(memories, now=NOW, ceiling=25)
    assert len(selected) == 25

    full_order = sorted(
        memories,
        key=lambda m: -memory_score(m, now=NOW, max_use_count=0),
    )
    expected_ids = [m["id"] for m in full_order[:25]]
    assert [m["id"] for m in selected] == expected_ids, (
        "selection must be exactly the top-25 by score, in score order, "
        "with no skipped-rank gaps"
    )
    scores = [memory_score(m, now=NOW, max_use_count=0) for m in selected]
    assert scores == sorted(scores, reverse=True)


def test_char_budget_fills_in_score_order():
    # 30 high-importance memories, long content, so _RULES_CRITICAL_CAP
    # (8000 chars) truncates the block before all 30 fit.
    memories = [
        _mem(
            str(i), tier="high", days_ago=i, use_count=0,
            title=f"title-{i}", content="x" * 400,
        )
        for i in range(30)
    ]
    selected = rank_and_select(memories, now=NOW, ceiling=25)
    block = build_memories_block(selected, critical_cap=8000, relevant_cap=4000)

    full_order = sorted(
        memories, key=lambda m: -memory_score(m, now=NOW, max_use_count=0),
    )
    ordered_titles = [m["title"] for m in full_order]
    present_titles = [t for t in ordered_titles if t in block]

    assert present_titles, "nothing made it into the block"
    assert len(present_titles) < 30, (
        "control: the char budget must actually bind, or this test proves "
        "nothing about ordering under pressure"
    )
    # Titles that made it into the block appear in the same relative order
    # they'd be selected in.
    positions = [block.index(t) for t in present_titles]
    assert positions == sorted(positions)


# --------------------------------------------------------------------------- #
# Ceiling
# --------------------------------------------------------------------------- #

def test_ceiling_is_hard_and_clips_not_raises():
    memories = [_mem(str(i), tier="med", days_ago=i, use_count=0)
                for i in range(100)]
    selected = rank_and_select(memories, now=NOW)
    assert len(selected) == 25
    assert MEMORY_CEILING == 25

    few = [_mem(str(i), tier="med", days_ago=i, use_count=0) for i in range(7)]
    selected_few = rank_and_select(few, now=NOW)
    assert len(selected_few) == 7


# --------------------------------------------------------------------------- #
# Floor
# --------------------------------------------------------------------------- #

def test_high_importance_top5_by_use_count_always_selected():
    # 5 high-importance rows: the highest use_count in the corpus, but the
    # STALEST last_used_at (worst possible score) so they'd otherwise lose on
    # score alone.
    floor_rows = [
        _mem(f"floor-{i}", tier="high", days_ago=3650, use_count=100 - i)
        for i in range(5)
    ]
    # A 6th high-importance row, ranked 6th by use_count within the tier —
    # must NOT be floored.
    rank6 = _mem("rank6", tier="high", days_ago=3650, use_count=50)
    # A pile of medium-tier rows with fresh, high-scoring recency/use so they
    # outrank the floor rows on pure score.
    fillers = [
        _mem(f"filler-{i}", tier="med", days_ago=0, use_count=1000)
        for i in range(60)
    ]

    corpus = floor_rows + [rank6] + fillers
    selected = rank_and_select(corpus, now=NOW, ceiling=25, floor_rank=5)

    selected_ids = [m["id"] for m in selected]
    for row in floor_rows:
        assert row["id"] in selected_ids, (
            f"{row['id']} is a top-5-by-use_count importance:high memory and "
            "must be floored in regardless of score"
        )
    assert "rank6" not in selected_ids, (
        "a 6th-ranked high-importance memory is not floor-eligible"
    )

    floor_positions = [selected_ids.index(r["id"]) for r in floor_rows]
    non_floor_positions = [
        i for i, mid in enumerate(selected_ids)
        if mid not in {r["id"] for r in floor_rows}
    ]
    assert max(floor_positions) < min(non_floor_positions), (
        "floored memories must appear before the score-ordered remainder"
    )


def test_floor_is_ranked_within_the_high_tier_not_globally():
    """The exact regression this module exists to prevent: a pile of
    medium-tier rows with enormous use_count must not crowd high-importance
    rows out of the floor just because a GLOBAL top-5-by-use_count would be
    all medium rows. Under a global-ranking floor, the 3 highs below have
    use_rank 11-13 (behind all 10 dominant meds) so the floor set would be
    empty and, with enough fresh high-use fillers to blow the ceiling, they
    would score their way out entirely.
    """
    highs = [
        _mem(f"high-{i}", tier="high", days_ago=3650, use_count=5 - i)
        for i in range(3)  # use_count 5, 4, 3; stale -> worst possible score
    ]
    dominant_meds = [
        _mem(f"med-{i}", tier="med", days_ago=0, use_count=50 + i)
        for i in range(10)  # use_count 50..59, globally dwarfs the highs
    ]
    fillers = [
        _mem(f"filler-{i}", tier="med", days_ago=0, use_count=1000)
        for i in range(20)  # push total candidate count past the ceiling
    ]
    corpus = highs + dominant_meds + fillers
    selected = rank_and_select(corpus, now=NOW, ceiling=25, floor_rank=5)
    assert len(selected) == 25, "control: the ceiling must actually bind here"
    selected_ids = {m["id"] for m in selected}
    for row in highs:
        assert row["id"] in selected_ids, (
            "all 3 high-importance rows have use_rank <= 5 WITHIN the high "
            "tier and must be floored in even though every medium row "
            "outranks them on GLOBAL use_count, and even under ceiling "
            "pressure that would otherwise score them out"
        )


# --------------------------------------------------------------------------- #
# Empty-ledger fallback
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("use_count_value", ["absent", 0])
def test_empty_ledger_falls_back_to_importance_times_recency(use_count_value):
    memories = []
    for i, tier in enumerate(["high", "med", "low"] * 4):
        m = _mem(f"m{i}", tier=tier, days_ago=i * 2)
        if use_count_value != "absent":
            m["use_count"] = use_count_value
        memories.append(m)

    selected = rank_and_select(memories, now=NOW)

    importance_weight = {"high": 1.0, "med": 0.5, "low": 0.2}

    def tier_of(m):
        if "importance:high" in m["tags"]:
            return "high"
        if "importance:low" in m["tags"]:
            return "low"
        return "med"

    # Frequency factor is 1.0 for everyone: max_use_count is 0, so score
    # collapses to importance_weight * recency with no frequency term.
    for m in memories:
        expected = (
            importance_weight[tier_of(m)]
            * math.exp(-((NOW - datetime.fromisoformat(m["last_used_at"]))
                          .total_seconds() / 86400.0) / 14.0)
        )
        assert memory_score(m, now=NOW, max_use_count=0) == pytest.approx(
            expected, abs=1e-12,
        )

    expected_order = sorted(
        memories,
        key=lambda m: (
            -(importance_weight[tier_of(m)]
              * math.exp(-(NOW - datetime.fromisoformat(m["last_used_at"])).days
                         / 14.0)),
            m["id"],
        ),
    )
    assert [m["id"] for m in selected] == [m["id"] for m in expected_order]

    # No arbitrary floor when there is no usable frequency signal at all.
    high_ids = {m["id"] for m in memories if tier_of(m) == "high"}
    # None of the highs are use-ranked (use_count is 0/absent everywhere), so
    # floor membership contributes nothing beyond plain score order — assert
    # by checking the selection order equals pure score order (already done
    # above), which would fail if 5 arbitrary highs were force-floored ahead
    # of a higher-scoring low/med row.
    assert high_ids  # control: the fixture does contain high rows


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #

def test_selection_is_identical_across_ten_runs():
    memories = [_mem(f"m{i}", tier="med", days_ago=5, use_count=10)
                for i in range(20)]  # identical tier/days/use_count -> ties
    memories += [_mem(f"h{i}", tier="high", days_ago=1, use_count=2)
                 for i in range(3)]

    runs = []
    for _ in range(10):
        shuffled = list(memories)
        random.Random(len(runs)).shuffle(shuffled)
        runs.append([m["id"] for m in rank_and_select(shuffled, now=NOW)])

    assert all(r == runs[0] for r in runs), "selection order is not stable"

    # Ties (identical score) resolved by ascending id.
    tied_ids = sorted(m["id"] for m in memories if m["id"].startswith("m"))
    selected_tied = [mid for mid in runs[0] if mid.startswith("m")]
    assert selected_tied == tied_ids


# --------------------------------------------------------------------------- #
# No LLM in the hot path
# --------------------------------------------------------------------------- #

def test_ranking_imports_no_vendor_client():
    result = subprocess.run(
        [sys.executable, "-c",
         "import no_human.learning.ranking, sys; "
         "print([m for m in sys.modules if 'anthropic' in m or 'httpx' in m])"],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "[]"


def test_rank_and_select_is_a_plain_sync_function():
    assert not inspect.iscoroutinefunction(rank_and_select)
    assert not inspect.isasyncgenfunction(rank_and_select)


# --------------------------------------------------------------------------- #
# Trigger integration
# --------------------------------------------------------------------------- #

async def test_scoring_only_reorders_the_triggered_set(tmp_path):
    from no_human.core.db import Store
    from no_human.core.orchestrator import Orchestrator
    from no_human.core.task import Task

    async with Store(tmp_path / "nh.db") as store:
        # High-scoring on paper (importance:high, tag matches) but its tag
        # does not appear anywhere in the task text -> must never trigger.
        never_triggers = await store.add_memory(
            mem_type="rule", title="css importance high",
            content="x", tags=["css", "importance:high"], confirmed=True)
        fires = await store.add_memory(
            mem_type="rule", title="kafka rule",
            content="x", tags=["kafka"], confirmed=True)

        task = Task.new("Fix the kafka topic creation", repo_path="")
        orch = Orchestrator.__new__(Orchestrator)
        orch.store = store
        all_scoped, triggered = await orch._load_active_memories(task)

        assert {m["id"] for m in all_scoped} == {never_triggers, fires}
        assert {m["id"] for m in triggered} == {fires}, (
            "control: the css rule must not even pass the trigger filter"
        )

        active_ids = {m["id"] for m in orch._active_memories}
        assert active_ids == {fires}
        assert active_ids <= {m["id"] for m in triggered}, (
            "scoring must only reorder the triggered set, never widen it"
        )
        assert never_triggers not in active_ids

        rows = {m["id"]: m for m in await store.list_memories(confirmed=True)}
        assert rows[fires]["last_used_at"], "the selected rule was never stamped"
        assert rows[never_triggers]["last_used_at"] is None, (
            "a memory that never triggered must never be stamped as used"
        )
