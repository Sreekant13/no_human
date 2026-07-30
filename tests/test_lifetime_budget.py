"""Lifetime task budget: the 21M-token / attempt-17 lesson.

`max_attempts` bounds ONE loop; every resume starts a fresh one. Task 84251cb2
reached attempt 17 and 21.2M cache-read tokens without any cap firing. The
lifetime budget stops that honestly: a BUDGET_EXHAUSTED blocker whose option
raises the budget for that one task — a human decision, never a retry's.
"""

from __future__ import annotations

import pytest

from no_human.blockers import BlockerCategory, apply_action, route_for
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


def _orch(store, cfg_extra=None):
    """A minimal orchestrator: only .store, .bounds and .emit are exercised."""
    from no_human.core.bounds import Bounds
    from no_human.core.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    o.store = store
    o.bounds = Bounds.from_config(cfg_extra or {})
    o._sink = lambda e: None
    return o


async def _spend(store, task_id, attempts, tokens_each):
    for n in range(1, attempts + 1):
        aid = await store.create_attempt(task_id, n)
        await store.update_attempt(aid, tokens_used=tokens_each // 2,
                                   cache_read_tokens=tokens_each - tokens_each // 2)


async def test_under_budget_returns_none_and_emits_the_running_total(store):
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)
    await _spend(store, t.id, attempts=2, tokens_each=1_000_000)

    events = []
    o = _orch(store)
    o._sink = events.append
    assert await o._check_lifetime_budget(t) is None
    ev = next(e for e in events if e["kind"] == "lifetime_budget")
    assert ev["attempts_used"] == 2
    assert ev["tokens_used"] == 2_000_000


async def test_attempt_17_would_have_parked_long_before(store):
    """The real run: 17 attempts. The default cap (9) fires at attempt 10."""
    t = Task.new("ci_gate", repo_path="/tmp/x")
    await store.create_task(t)
    await _spend(store, t.id, attempts=9, tokens_each=100)

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None
    assert b.category is BlockerCategory.BUDGET_EXHAUSTED
    assert "attempts 9/9" in b.root_cause_hypothesis
    # Routed to a human, never auto-retried.
    r = route_for(b.category)
    assert r.target_status is TaskStatus.ESCALATED and not r.auto_retry


async def test_token_cap_fires_even_with_few_attempts(store):
    """One monster attempt (the 3.4M-cache-read shape) must count."""
    t = Task.new("burn", repo_path="/tmp/x")
    await store.create_task(t)
    await _spend(store, t.id, attempts=2, tokens_each=13_000_000)  # 26M > 8M cap

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None
    assert "tokens" in b.root_cause_hypothesis


async def test_default_token_cap_parks_a_9M_burn(store):
    """The 8M default (measured 2026-07-13): the largest PR-producing task in the
    corpus burned 6.15M; everything else that succeeded was ≤1.71M. Only the parked
    CI_GATE/metrics-core runaways (20.8M, 61.5M) sat above 8M. A ~9M task must park under the
    DEFAULT — it would NOT have parked at the old 25M cap."""
    from no_human.core.bounds import Bounds
    assert Bounds().lifetime_tokens == 8_000_000

    t = Task.new("nine-million", repo_path="/tmp/x")
    await store.create_task(t)
    await _spend(store, t.id, attempts=2, tokens_each=4_500_000)  # 9M > 8M, < old 25M

    b = await _orch(store)._check_lifetime_budget(t)  # default bounds, no override
    assert b is not None and "tokens" in b.root_cause_hypothesis


async def test_the_raise_option_actually_raises_and_unblocks(store):
    """The SCOPE_EXPLOSION lesson: an option must carry the action that makes
    it true. Applying it and re-checking must clear the blocker."""
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)
    await _spend(store, t.id, attempts=9, tokens_each=100)

    o = _orch(store)
    b = await o._check_lifetime_budget(t)
    raise_opt = next(opt for opt in b.options if opt.action)
    summary = apply_action(t, raise_opt.action)
    assert "lifetime_attempts" in summary

    assert await o._check_lifetime_budget(t) is None, (
        "after the human raises the budget, the same check must pass"
    )


async def test_per_task_override_wins_over_config(store):
    t = Task.new("task", repo_path="/tmp/x")
    t.config = {"lifetime_attempts": 2}
    await store.create_task(t)
    await _spend(store, t.id, attempts=2, tokens_each=100)

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None and "attempts 2/2" in b.root_cause_hypothesis


async def test_killed_rows_with_zero_tokens_still_count_as_attempts(store):
    """Pre-1638427 rows recorded zero tokens; they still spent the attempt."""
    t = Task.new("task", repo_path="/tmp/x")
    await store.create_task(t)
    for n in range(1, 10):
        await store.create_attempt(t.id, n)  # no token columns at all

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None and "attempts 9/9" in b.root_cause_hypothesis


# --------------------------------------------------------------------------- #
# What the gate COUNTS.
#
# `lifetime_usage` summed two of the attempts table's twelve token columns:
# tokens_used + cache_read_tokens. It ignored every cache_creation_* column and
# all three of the review_*, plan_* and utility_* tiers — so the gate that
# exists to stop runaway spend could not see reviewer, planner or utility burn
# at all. eval/northstar.py already summed all four tiers, so the budget gate
# and the benchmark were reporting different numbers for the same task.
#
# Measured over 574 real attempt rows (184 tasks): the ignored columns are
# 16.2% of true spend, and utility_cache_creation_tokens alone (60.1M) exceeds
# the coder's own cache_creation_tokens (59.0M).
# --------------------------------------------------------------------------- #

_TIERS = ("", "review_", "plan_", "utility_")


async def test_every_tier_and_both_cache_columns_reach_the_gate(store):
    """One attempt, a distinct power of ten in each of the twelve columns, so
    any dropped column changes the total by a recognisable amount."""
    t = Task.new("all tiers", repo_path="/tmp/x")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)

    cols, expected = {}, 0
    for i, tier in enumerate(_TIERS):
        base = "tokens_used" if tier == "" else f"{tier}tokens_used"
        for j, suffix in enumerate((base, f"{tier}cache_read_tokens",
                                    f"{tier}cache_creation_tokens")):
            value = 10 ** (i * 3 + j + 1)
            cols[suffix] = value
            expected += value
    await store.update_attempt(aid, **cols)

    attempts, tokens = await store.lifetime_usage(t.id)
    assert attempts == 1
    assert tokens == expected, (
        f"gate saw {tokens:,} of {expected:,} actually spent; "
        f"missing {expected - tokens:,}"
    )


async def test_a_task_whose_spend_is_all_reviewer_is_not_invisible(store):
    """The pathological shape: a task that burns its whole budget in the
    reviewer tier used to read as ZERO to the gate and could never be parked."""
    t = Task.new("review burn", repo_path="/tmp/x")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        aid, review_tokens_used=1_000_000,
        review_cache_read_tokens=8_000_000,
        review_cache_creation_tokens=500_000,
    )

    _, tokens = await store.lifetime_usage(t.id)
    assert tokens == 9_500_000

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None and b.category is BlockerCategory.BUDGET_EXHAUSTED


async def test_the_gate_and_the_benchmark_agree(store):
    """eval/northstar.py sums all four tiers to report cost. The budget gate
    must report the same number for the same rows, or one of the two is lying."""
    t = Task.new("agreement", repo_path="/tmp/x")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    cols = {}
    for tier in _TIERS:
        cols["tokens_used" if tier == "" else f"{tier}tokens_used"] = 7
        cols[f"{tier}cache_read_tokens"] = 70
        cols[f"{tier}cache_creation_tokens"] = 700
    await store.update_attempt(aid, **cols)

    # Recomputed the way eval/northstar.py does it — over the stored ROWS, not
    # by calling the function under test.
    rows = await store.list_attempts(t.id)
    northstar_total = 0
    for r in rows:
        for tier in _TIERS:
            northstar_total += int(r[("tokens_used" if tier == ""
                                      else f"{tier}tokens_used")] or 0)
            northstar_total += int(r[f"{tier}cache_read_tokens"] or 0)
            northstar_total += int(r[f"{tier}cache_creation_tokens"] or 0)

    _, gate_total = await store.lifetime_usage(t.id)
    assert gate_total == northstar_total == 4 * (7 + 70 + 700)
