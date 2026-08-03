"""Lifetime task budget: the 21M-token / attempt-17 lesson.

`max_attempts` bounds ONE loop; every resume starts a fresh one. Task 84251cb2
reached attempt 17 and 21.2M cache-read tokens without any cap firing. The
lifetime budget stops that honestly: a BUDGET_EXHAUSTED blocker whose option
raises the budget for that one task — a human decision, never a retry's.
"""

from __future__ import annotations

import pytest

from no_human.blockers import BlockerCategory, apply_action, route_for
from no_human.core.bounds import Bounds
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
    """The 8M-raw default (measured 2026-07-13): the largest PR-producing task in
    the corpus burned 6.15M; everything else that succeeded was ≤1.71M. Only the
    parked CI_GATE/metrics-core runaways (20.8M, 61.5M) sat above 8M. A ~9M raw task
    must park under the DEFAULT — it would NOT have parked at the old 25M cap.

    The cap is COST-WEIGHTED since 2026-07-31, and RAISED to 4M on 2026-08-03
    from the honest-ledger sweep (the 1.6M conversion was calibrated on the
    pre-fix ledger whose subagent spend was under-counted; derivation on
    core.bounds.Bounds).
    This test's 9M-raw burn is 4.95M weighted and must STILL park under the
    raised default — it is the smallest runaway class the cap exists for, and
    a raise that spares it has gone too far."""
    from no_human.core.bounds import Bounds
    assert Bounds().lifetime_tokens == 4_000_000

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
        aid, review_tokens_used=2_000_000,
        review_cache_read_tokens=16_000_000,
        review_cache_creation_tokens=1_000_000,
    )

    _, tokens = await store.lifetime_usage(t.id)
    assert tokens == 19_000_000

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


# --------------------------------------------------------------------------- #
# What the gate PRICES.
#
# Counting the three token classes 1:1 does not bound spend, it bounds
# conversation LENGTH. Cache reads bill at a tenth of fresh input and cache
# writes at 1.25x, so a raw cap fires an order of magnitude before the dollar
# cost it exists to bound — and it fires at a DIFFERENT dollar cost for every
# task, because the per-task weighted/raw ratio ranges 0.122..0.697 over this
# install's ledger.
#
# The live casualty: task d6e4b72a was killed BUDGET_EXHAUSTED at
# "12,367,237/12,000,000 tokens" after 22 minutes. Two measurements of it are
# used below, and they are different snapshots of the same run — both real,
# neither reconstructed:
#
#   * D6E4B72A_ATTEMPT — the usage events of the killing attempt, summed
#     (the numbers quoted in the incident: 1,697 / 6,589,429 / 173,190).
#   * D6E4B72A_LEDGER — the task's four attempt ROWS as the DB holds them,
#     per class, read out of ~/.no_human/no_human.db on 2026-07-31. Larger
#     than the 12,367,237 it died at, because an aborted attempt banks its
#     whole spend to the row afterwards.
# --------------------------------------------------------------------------- #

from no_human.core.pricing import (  # noqa: E402
    CACHE_CREATION_WEIGHT, CACHE_READ_WEIGHT, FRESH_WEIGHT,
    class_breakdown, weighted_tokens,
)

#: The killing attempt's usage events, summed. Raw 6,764,316.
D6E4B72A_ATTEMPT = {
    "tokens_used": 1_697,
    "cache_read_tokens": 6_589_429,
    "cache_creation_tokens": 173_190,
}
#: The same task's four attempt rows, per class. Raw 16,527,553.
D6E4B72A_LEDGER = {
    "tokens_used": 65_968,
    "cache_read_tokens": 15_005_665,
    "cache_creation_tokens": 1_455_920,
}
#: The cap it actually ran under (a human raise, in task.config).
D6E4B72A_CAP = 12_000_000


def test_each_token_class_is_weighted_by_its_own_price():
    """One class at a time, so a dropped or transposed term is visible."""
    assert weighted_tokens(tokens_used=1_000_000) == 1_000_000
    assert weighted_tokens(cache_read_tokens=1_000_000) == 100_000
    assert weighted_tokens(cache_creation_tokens=1_000_000) == 1_250_000
    # Nothing is free and nothing is double-counted: the mix is the sum.
    assert weighted_tokens(tokens_used=1_000_000, cache_read_tokens=1_000_000,
                           cache_creation_tokens=1_000_000) == 2_350_000
    assert weighted_tokens() == 0
    # The classes are NOT interchangeable — 12.5x apart end to end. A
    # positional call could not have produced this asymmetry.
    assert (weighted_tokens(cache_creation_tokens=1_000)
            == 12 * weighted_tokens(cache_read_tokens=1_000) + 50)
    # The weights themselves, so a silent re-pricing is a test failure and not
    # a quiet change in what every cap means.
    assert (FRESH_WEIGHT, CACHE_CREATION_WEIGHT, CACHE_READ_WEIGHT) == (
        1.0, 1.25, 0.1)


def test_known_event_mixes_price_the_way_the_incident_did():
    """The two d6e4b72a snapshots, priced. Both are ~5x cheaper than raw."""
    assert sum(D6E4B72A_ATTEMPT.values()) == 6_764_316          # raw
    assert weighted_tokens(**D6E4B72A_ATTEMPT) == 877_127       # real
    assert sum(D6E4B72A_LEDGER.values()) == 16_527_553          # raw
    assert weighted_tokens(**D6E4B72A_LEDGER) == 3_386_434      # real
    # A purely fresh burn is priced at face value — the weighting is not a
    # blanket discount, it is a class distinction.
    assert weighted_tokens(tokens_used=6_764_316) == 6_764_316


async def test_d6e4b72a_would_not_have_been_killed_when_it_was(store):
    """The regression the fix exists for, at the moment it fired.

    The task carried an UNMARKED raw 12,000,000 raise — its real stored
    config — so the cutover guard reads that grant as 2,382,000 weighted. At
    the kill, the running attempt had spent 6,764,316 raw / 877,127
    fresh-equivalent: over the raw ceiling that stopped it, and comfortably
    under the grant it actually held. It would have kept going.
    """
    t = Task.new("Silent task failure: a pool crash records no reason",
                 repo_path="/tmp/x")
    t.config = {"lifetime_tokens": D6E4B72A_CAP}          # raw, unmarked
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, **D6E4B72A_ATTEMPT)

    o = _orch(store)
    assert o._lifetime_limits(t)[1] == 2_382_000, "grant read in its own unit"
    _, raw = await store.lifetime_usage(t.id)
    assert raw == 6_764_316 > 2_382_000, "the raw sum is what stopped it"

    assert await o._check_lifetime_budget(t) is None, (
        "the task killed at 12,367,237/12,000,000 must survive: its real "
        f"spend was {weighted_tokens(**D6E4B72A_ATTEMPT):,} fresh-equivalent"
    )


async def test_d6e4b72a_is_not_handed_a_blank_cheque_either(store):
    """The honest other half, recorded so the fix is not oversold.

    Across all FOUR of its attempts the task spent 3,386,434 weighted against
    a grant worth 2,382,000. It does park — later, and on a true reading of
    what it cost. Re-pricing the counter moved WHEN the gate fires; it did not
    exempt an expensive task from it.
    """
    t = Task.new("d6e4b72a, all four attempts", repo_path="/tmp/x")
    t.config = {"lifetime_tokens": D6E4B72A_CAP}          # raw, unmarked
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, **D6E4B72A_LEDGER)

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None and b.category is BlockerCategory.BUDGET_EXHAUSTED
    assert "3,386,434/2,382,000" in b.root_cause_hypothesis


async def test_a_genuinely_expensive_task_still_parks_at_the_same_cap(store):
    """The other side, and the reason this is a re-pricing and not a raise.

    The IDENTICAL raw burn to the d6e4b72a ledger above — 16,527,553 tokens to
    the token — under the identical 12,000,000 cap, but spent on fresh input
    and cache writes instead of prefix re-reads. Opposite verdict, which is the
    whole point: the gate now discriminates on cost, where before these two
    were indistinguishable and it parked both.
    """
    expensive = {"tokens_used": 14_600_000, "cache_read_tokens": 1_027_553,
                 "cache_creation_tokens": 900_000}
    assert sum(expensive.values()) == sum(D6E4B72A_LEDGER.values())
    assert weighted_tokens(**expensive) == 15_827_755 > D6E4B72A_CAP

    t = Task.new("expensive", repo_path="/tmp/x")
    # Marked: this compares against 12,000,000 as a WEIGHTED cap, which is the
    # post-cutover meaning of that number.
    t.config = {"lifetime_tokens": D6E4B72A_CAP, "budget_unit": "weighted"}
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, **expensive)

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None and b.category is BlockerCategory.BUDGET_EXHAUSTED
    assert "cost-weighted tokens 15,827,755/12,000,000" in b.root_cause_hypothesis


async def test_the_blocker_reports_the_weighted_number_and_the_raw_classes(store):
    """Honest reporting: the operator must see the number the gate acted on AND
    the raw split it came from, or they cannot reconcile the park against a
    bill or against `nh logs`."""
    t = Task.new("park me", repo_path="/tmp/x")
    t.config = {"lifetime_tokens": 1_000_000, "budget_unit": "weighted"}
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, **D6E4B72A_LEDGER)

    b = await _orch(store)._check_lifetime_budget(t)
    assert b is not None
    assert "3,386,434" in b.evidence and "cost-weighted" in b.evidence
    # Every raw class, named and priced, not just their sum.
    for raw_value in ("65,968", "15,005,665", "1,455,920"):
        assert raw_value in b.evidence, f"{raw_value} missing from {b.evidence}"
    for label in ("fresh", "cache-write", "cache-read"):
        assert label in b.evidence
    # The raise option is in the same unit as the cap it raises.
    raise_opt = next(o for o in b.options if o.action)
    assert "cost-weighted" in raise_opt.label
    # 3,386,434 x 1.5, rounded up to the next 100,000.
    assert raise_opt.action["set_task_config"]["lifetime_tokens"] == 5_100_000


async def test_the_under_budget_event_carries_both_numbers(store):
    """The passing path reports the same two quantities the blocker does — and
    keeps `tokens_used` RAW, because that name means the raw burn on every
    other surface (`nh`, the web meters, eval/northstar.py)."""
    t = Task.new("running", repo_path="/tmp/x")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, **D6E4B72A_ATTEMPT)

    events: list = []
    o = _orch(store)
    o._sink = events.append
    assert await o._check_lifetime_budget(t) is None
    ev = next(e for e in events if e["kind"] == "lifetime_budget")
    assert ev["tokens_used"] == 6_764_316          # raw, unchanged meaning
    assert ev["tokens_weighted"] == 877_127        # what the cap compares
    assert ev["raw_fresh"] == 1_697
    assert ev["raw_cache_read"] == 6_589_429
    assert ev["raw_cache_creation"] == 173_190
    assert "877,127" in ev["text"] and "4,000,000" in ev["text"]
    # web/src/summaries.js clips this text to 60 chars for the Activity
    # header's "Budget" fact. Anything longer renders truncated mid-number,
    # which is why the class split lives in the fields above and not in here.
    assert len(ev["text"]) <= 60, f"{len(ev['text'])} chars: {ev['text']!r}"


async def test_the_class_split_sees_every_tier_and_column(store):
    """`lifetime_usage_by_class` must reach all twelve ADDEND columns, like
    `lifetime_usage` — a tier missed here is spend the price weighting never
    sees. Distinct powers of ten so any dropped column is identifiable.

    Plus the four `*output_tokens` columns, which are NOT addends: each is the
    output slice of the `*tokens_used` column beside it. They are set to that
    same value here — an all-output attempt, which is coherent and keeps the
    powers of ten distinct — so a dropped output column is identifiable the
    same way, while the raw total must not move by a single token."""
    t = Task.new("all tiers", repo_path="/tmp/x")
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)

    cols, per_class = {}, {"tokens_used": 0, "cache_read_tokens": 0,
                           "cache_creation_tokens": 0}
    for i, tier in enumerate(_TIERS):
        base = "tokens_used" if tier == "" else f"{tier}tokens_used"
        for j, (col, cls) in enumerate((
            (base, "tokens_used"),
            (f"{tier}cache_read_tokens", "cache_read_tokens"),
            (f"{tier}cache_creation_tokens", "cache_creation_tokens"),
        )):
            value = 10 ** (i * 3 + j + 1)
            cols[col] = value
            per_class[cls] += value
        cols[f"{tier}output_tokens"] = cols[base]
    await store.update_attempt(aid, **cols)

    attempts, by_class = await store.lifetime_usage_by_class(t.id)
    assert attempts == 1
    assert {k: v for k, v in by_class.items() if k != "output_tokens"} == per_class
    # Every output column reached, and none double-counted: an attempt that is
    # 100% output reports exactly the fresh class as its output share.
    assert by_class["output_tokens"] == per_class["tokens_used"]
    # And the raw total is still exactly the sum of the three ADDEND classes,
    # so the burn figure every other surface shows cannot drift from the priced
    # one — the output slice is inside `tokens_used`, never beside it.
    _, raw = await store.lifetime_usage(t.id)
    assert raw == sum(per_class.values())
    # The priced number, by contrast, MUST move: that is the defect this
    # column exists to close. An all-output fresh class bills 5x, not 1x.
    assert weighted_tokens(**by_class) == weighted_tokens(
        **per_class) + 4 * per_class["tokens_used"]


async def test_the_mid_attempt_watch_prices_the_same_way_the_gate_does(store):
    """The sink's running total is the second enforcement point. It must reach
    the ceiling on the same arithmetic, or the mid-attempt abort fires where
    the boundary check would have said the task was fine — which is exactly
    what killed d6e4b72a 22 minutes in."""
    from no_human.core.orchestrator import BudgetAbort, Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    o._sink = lambda e: None
    o._cancel_reason = None
    o._active_task_id = "task-1"
    # The ceiling d6e4b72a's killing attempt actually ran under: what was left
    # of its 12,000,000 after 5,602,921 of earlier spend had been banked.
    o._begin_attempt_accounting("task-1", remaining_tokens=12_000_000 - 5_602_921)

    from no_human.agent.claude_backend import AgentEvent
    from no_human.core.orchestrator import CODER_ROLE
    # The killing attempt's own usage, delivered as one event. Raw 6,764,316
    # is over the 6,397,079 ceiling — that is the abort that happened.
    assert sum(D6E4B72A_ATTEMPT.values()) > 6_397_079
    o._agent_sink(AgentEvent("usage", meta=dict(D6E4B72A_ATTEMPT)),
                  role=CODER_ROLE)  # priced at 877,127 — must NOT abort

    # Same ceiling, a fresh-heavy attempt of comparable raw size: aborts.
    o._begin_attempt_accounting("task-1", remaining_tokens=12_000_000 - 5_602_921)
    with pytest.raises(BudgetAbort) as exc:
        o._agent_sink(AgentEvent("usage", meta={
            "tokens_used": 6_000_000, "cache_read_tokens": 400_000,
            "cache_creation_tokens": 400_000}), role=CODER_ROLE)
    # And it says both numbers, for the same honest-reporting reason.
    assert "6,540,000 cost-weighted" in str(exc.value)
    assert "raw 6,800,000" in str(exc.value)


def test_the_class_breakdown_names_and_prices_every_class():
    text = class_breakdown(**D6E4B72A_ATTEMPT)
    assert "raw 6,764,316" in text
    assert "1,697 fresh (x1)" in text
    assert "173,190 cache-write (x1.25)" in text
    assert "6,589,429 cache-read (x0.1)" in text


# --------------------------------------------------------------------------- #
# The CUTOVER guard.
#
# The caps changed UNIT on 2026-07-31. Measured on the live ledger that day:
# 165 tasks carry a `task.config["lifetime_tokens"]` written in RAW tokens
# (12,000,000 to 68,000,000) and 162 carry a raw `attempt_tokens` (4M or 6M).
# They are not stale history — 94 of the 165 sit in escalated / failed /
# paused_quota / implementing, i.e. exactly what a mass retry picks up, and
# across the 91 escalated+failed alone the stored ceilings total 1.388 BILLION
# where ~275M was intended.
#
# Read verbatim against a weighted gate, every one of those is ~5x the budget
# the human granted. And `blockers/actions.py` never lowers a stored cap, so
# without a unit-aware comparison the stale value is PERMANENT: the raise flow
# that exists to fix a budget would keep re-affirming it.
#
# The guard is a read-time interpretation, not a migration: nothing is written
# to anyone's database, and there is no ordering requirement between deploying
# the code and fixing the data.
# --------------------------------------------------------------------------- #

from no_human.core.pricing import (  # noqa: E402
    BUDGET_UNIT_KEY, RAW_TO_WEIGHTED_RATIO, TOKEN_CAP_KEYS, WEIGHTED_UNIT,
    config_is_weighted, raw_cap_as_weighted,
)

_MARKED = {BUDGET_UNIT_KEY: WEIGHTED_UNIT}


def test_an_unmarked_stored_cap_is_read_as_raw_and_converted():
    """The reviewer's red-green case: the same stored 12,000,000 means two
    different budgets, and which one it means is decided by the marker."""
    from no_human.core.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    o.bounds = Bounds()

    stale = Task.new("pre-cutover", repo_path="/tmp/x")
    stale.config = {"lifetime_tokens": 12_000_000}
    assert o._lifetime_limits(stale)[1] == 2_382_000

    fresh = Task.new("post-cutover", repo_path="/tmp/x")
    fresh.config = {"lifetime_tokens": 12_000_000, **_MARKED}
    assert o._lifetime_limits(fresh)[1] == 12_000_000

    # BOTH keys, not just the loud one: 162 rows carry a raw attempt_tokens.
    stale.config = {"attempt_tokens": 4_000_000}
    assert o._attempt_token_cap(stale) == 794_000
    fresh.config = {"attempt_tokens": 4_000_000, **_MARKED}
    assert o._attempt_token_cap(fresh) == 4_000_000
    assert TOKEN_CAP_KEYS == {"lifetime_tokens", "attempt_tokens"}


def test_the_marker_check_fails_closed_on_anything_that_is_not_the_marker():
    """Absence is the safe state, because the unsafe direction spends money."""
    assert config_is_weighted({BUDGET_UNIT_KEY: WEIGHTED_UNIT}) is True
    for hostile in (None, {}, {"budget_unit": "raw"}, {"budget_unit": True},
                    {"budget_unit": "WEIGHTED"}, {"lifetime_tokens": 1}, "weighted"):
        assert config_is_weighted(hostile) is False, hostile


def test_the_conversion_is_the_measured_corpus_ratio():
    assert RAW_TO_WEIGHTED_RATIO == 0.1985
    assert raw_cap_as_weighted(12_000_000) == 2_382_000
    assert raw_cap_as_weighted(68_000_000) == 13_498_000   # the largest stale row
    # Never rounds an override away to "no override" — that would silently
    # RAISE a deliberately tiny cap to the default instead of lowering it.
    from no_human.core.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o.bounds = Bounds()
    tiny = Task.new("tiny", repo_path="/tmp/x")
    tiny.config = {"lifetime_tokens": 3}
    assert o._lifetime_limits(tiny)[1] == 1
    assert o._lifetime_limits(tiny)[1] < Bounds().lifetime_tokens


def test_a_stale_raw_ceiling_is_correctable_and_does_not_become_permanent():
    """`actions.py` never lowers a stored cap. Comparing a weighted request
    against an unconverted raw prior means the raw one always wins and is then
    written back stamped as weighted — freezing the 5x forever, through the
    very flow that exists to correct a budget."""
    t = Task.new("stale", repo_path="/tmp/x")
    t.config = {"lifetime_tokens": 12_000_000, "attempt_tokens": 4_000_000}

    summary = apply_action(t, {"set_task_config": {"lifetime_tokens": 5_100_000}})

    assert t.config["lifetime_tokens"] == 5_100_000, summary
    assert t.config[BUDGET_UNIT_KEY] == WEIGHTED_UNIT
    # ...and the never-lower guard is INTACT, not disabled: a request under the
    # converted prior (2,382,000) is still refused.
    t2 = Task.new("stale2", repo_path="/tmp/x")
    t2.config = {"lifetime_tokens": 12_000_000}
    apply_action(t2, {"set_task_config": {"lifetime_tokens": 1_000_000}})
    assert t2.config["lifetime_tokens"] == 2_382_000
    assert t2.config[BUDGET_UNIT_KEY] == WEIGHTED_UNIT


def test_the_marker_is_stamped_by_the_write_path_not_asked_of_callers():
    """Every sanctioned write lands in apply_action; a caller that forgot the
    marker would shrink its own cap 5x on the next read."""
    t = Task.new("t", repo_path="/tmp/x")
    apply_action(t, {"set_task_config": {"attempt_tokens": 900_000}})
    assert t.config[BUDGET_UNIT_KEY] == WEIGHTED_UNIT
    # The human CLI path stamps it too.
    t2 = Task.new("t2", repo_path="/tmp/x")
    apply_action(t2, {"set_task_config": {"lifetime_tokens": 5}},
                 human_override=True)
    assert t2.config[BUDGET_UNIT_KEY] == WEIGHTED_UNIT
    # A non-token cap must NOT claim a token unit it does not have.
    t3 = Task.new("t3", repo_path="/tmp/x")
    apply_action(t3, {"set_task_config": {"lifetime_attempts": 12}})
    assert BUDGET_UNIT_KEY not in t3.config


def test_a_repo_profile_default_is_covered_by_the_same_guard():
    """MEDIUM-1: `profile.apply_default_task_config` copies repo-level defaults
    straight into task.config and writes no marker, so those are raw too. Unset
    on this install, but it is the same field the 165 stale rows came through —
    confirmed, not assumed."""
    from no_human.profile import ProjectProfile, apply_default_task_config
    from no_human.core.orchestrator import Orchestrator

    prof = ProjectProfile(repo_path="/r", default_attempt_tokens=6_000_000,
                          default_lifetime_tokens=16_000_000)
    cfg = apply_default_task_config(prof, {})
    assert BUDGET_UNIT_KEY not in cfg, "profile defaults carry no unit marker"

    o = Orchestrator.__new__(Orchestrator)
    o.bounds = Bounds()
    t = Task.new("from profile", repo_path="/tmp/x")
    t.config = cfg
    assert o._lifetime_limits(t)[1] == 3_176_000    # 16M raw, converted
    assert o._attempt_token_cap(t) == 1_191_000     # 6M raw, converted


async def test_the_armed_mid_attempt_ceiling_is_in_the_weighted_unit(store):
    """MEDIUM-2: the third enforcement point — arming the sink's ceiling from
    the lifetime ledger — had no test at all, and the failure mode is severe
    and silent. `max(cap_life - used_life, 1)` with a RAW used_life against a
    weighted cap goes negative, clamps to a 1-token ceiling, and the attempt
    dies on its first usage event."""
    t = Task.new("armed", repo_path="/tmp/x")
    t.config = {"lifetime_tokens": 4_000_000, **_MARKED}
    await store.create_task(t)
    aid = await store.create_attempt(t.id, 1)
    await store.update_attempt(aid, **D6E4B72A_LEDGER)   # 16,527,553 raw

    from no_human.core.orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o.store = store
    o.bounds = Bounds()
    o._sink = lambda e: None

    # Drives the PRODUCTION arming path. The first version of this test
    # recomputed `max(cap - used, 1)` itself and asserted on the result, so
    # swapping the weighted ledger read for the raw one inside the orchestrator
    # left it green — a test that recomputes the code under test proves
    # nothing, which is this repo's own recorded lesson.
    await o._arm_attempt_budget(t)

    # 4,000,000 - 3,386,434 = 613,566 of real budget left, NOT the one-token
    # clamp a raw read produces (4,000,000 - 16,527,553 < 0).
    assert o._token_ceiling == (t.id, 613_566, "the task's remaining lifetime budget")
    assert o._token_ceiling[1] > 1
    assert weighted_tokens(**(await store.lifetime_usage_by_class(t.id))[1]) == 3_386_434


async def test_a_partial_write_cannot_promote_the_key_it_did_not_touch(store):
    """A TWO-KEY stale config — the shape the earlier marker tests all missed
    by starting from `Task.new()`, whose config is empty, so nothing
    pre-existing could be mis-stamped.

    `budget_unit` describes the whole dict, so stamping it makes a claim about
    every token cap in it. The BUDGET_EXHAUSTED raise option writes
    `{lifetime_attempts, lifetime_tokens}` and never `attempt_tokens`, and 162
    tasks on this install carry both keys in raw units — so the partial write
    is the ORDINARY path, not a corner case.

    The invariant asserted here is the general one, not two literals: a write
    to one cap must not change what the OTHER cap means.
    """
    from no_human.core.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    o.bounds = Bounds()
    stale = {"lifetime_tokens": 12_000_000, "attempt_tokens": 6_000_000}

    # -- direction A: the blocker's raise option touches lifetime only -------
    t = Task.new("stale two-key", repo_path="/tmp/x")
    t.config = dict(stale)
    assert (o._lifetime_limits(t)[1], o._attempt_token_cap(t)) == (2_382_000, 1_191_000)

    apply_action(t, {"set_task_config": {"lifetime_attempts": 12,
                                         "lifetime_tokens": 5_100_000}})

    assert o._lifetime_limits(t)[1] == 5_100_000            # the key it wrote
    assert o._attempt_token_cap(t) == 1_191_000, (          # the key it did NOT
        "a lifetime raise silently promoted the untouched raw attempt_tokens "
        f"to {o._attempt_token_cap(t):,}; config={t.config}")
    assert t.config[BUDGET_UNIT_KEY] == WEIGHTED_UNIT
    # The consequence that actually bites: an inflated attempt cap at or above
    # the remaining lifetime budget makes the per-attempt brake inert and the
    # bounded loop degenerates to one attempt (the v6-taxonomy failure
    # `Bounds.attempt_tokens` exists to prevent).
    assert o._attempt_token_cap(t) < o._lifetime_limits(t)[1]

    # -- direction B: the human CLI touches attempt only ---------------------
    t2 = Task.new("stale two-key reverse", repo_path="/tmp/x")
    t2.config = dict(stale)
    apply_action(t2, {"set_task_config": {"attempt_tokens": 800_000}},
                 human_override=True)
    assert t2.config["attempt_tokens"] == 800_000
    assert o._lifetime_limits(t2)[1] == 2_382_000, (
        "an attempt-cap write silently promoted the untouched raw "
        f"lifetime_tokens to {o._lifetime_limits(t2)[1]:,}; config={t2.config}")

    # -- an ALREADY-marked config is never converted a second time ----------
    t3 = Task.new("already weighted", repo_path="/tmp/x")
    t3.config = {**stale, BUDGET_UNIT_KEY: WEIGHTED_UNIT}
    apply_action(t3, {"set_task_config": {"lifetime_tokens": 20_000_000}})
    assert t3.config["attempt_tokens"] == 6_000_000, "double-converted"
    assert o._attempt_token_cap(t3) == 6_000_000


def test_a_write_never_changes_what_an_untouched_cap_means():
    """The invariant above, swept over every single-key write and both marker
    states, so a future key added to TOKEN_CAP_KEYS is covered by construction
    rather than by someone remembering to add a case."""
    from no_human.core.orchestrator import Orchestrator

    o = Orchestrator.__new__(Orchestrator)
    o.bounds = Bounds()
    read = {"lifetime_tokens": lambda t: o._lifetime_limits(t)[1],
            "attempt_tokens": o._attempt_token_cap}
    assert set(read) == TOKEN_CAP_KEYS, "a cap key is missing from this sweep"

    base = {"lifetime_tokens": 12_000_000, "attempt_tokens": 6_000_000}
    for marked in (False, True):
        for written in TOKEN_CAP_KEYS:
            for override in (False, True):
                cfg = dict(base)
                if marked:
                    cfg[BUDGET_UNIT_KEY] = WEIGHTED_UNIT
                t = Task.new("sweep", repo_path="/tmp/x")
                t.config = cfg
                untouched = next(iter(TOKEN_CAP_KEYS - {written}))
                before = read[untouched](t)
                apply_action(t, {"set_task_config": {written: 3_000_000}},
                             human_override=override)
                after = read[untouched](t)
                assert before == after, (
                    f"writing {written} changed {untouched}: {before:,} -> "
                    f"{after:,} (marked={marked}, override={override})")
