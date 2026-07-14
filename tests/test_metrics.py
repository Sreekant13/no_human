"""The measurement spine: north-star numbers straight from the record."""

from __future__ import annotations

import time

import pytest

from no_human.core.db import Store
from no_human.core.metrics import compute_metrics
from no_human.core.task import Task


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "nh.db").connect()
    yield s
    await s.close()


def _ev(kind: str, **extra) -> dict:
    return {"source": "test", "kind": kind, "text": extra.pop("text", ""),
            "ts": time.time(), **extra}


async def test_empty_db_yields_zeros_not_crashes(store):
    m = await compute_metrics(store)
    assert m["prs_opened"] == 0 and m["prs_merged"] == 0
    assert m["attempts_per_pr"] is None and m["tokens_per_pr"] is None
    assert m["error_breakdown"] == {}


async def test_error_breakdown_groups_agent_errors_by_class(store):
    """0.2/0.3: /api/metrics surfaces WHERE attempts waste tokens — refusal
    (fail-fast) vs retryable rate_limited vs a genuine error; pre-0.3 events
    with no class fall into 'unclassified'."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [
        _ev("agent_error", error_class="refusal"),
        _ev("agent_error", error_class="refusal"),
        _ev("agent_error", error_class="rate_limited"),
        _ev("agent_error"),  # no class → unclassified
        _ev("review", passed=1),  # unrelated kind must not count
    ])
    eb = (await compute_metrics(store))["error_breakdown"]
    assert eb == {"refusal": 2, "rate_limited": 1, "unclassified": 1}


async def test_the_north_star_numbers_add_up(store):
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    a1 = await store.create_attempt(t.id, attempt_number=1)
    a2 = await store.create_attempt(t.id, attempt_number=2)
    await store.update_attempt(a1, tokens_used=100, cache_read_tokens=1000,
                               auth_profile="personal")
    await store.update_attempt(a2, tokens_used=50, cache_read_tokens=500,
                               auth_profile="personal",
                               pr_url="https://forge/pr/1")
    await store.save_events(t.id, [
        _ev("review", passed=0), _ev("review", passed=1),
        _ev("merged", text="PR merged by a human"),
        _ev("repro_gate", verdict="waived"),
        _ev("attempt_failed", text="review failed: off-by-one in parser"),
    ])

    m = await compute_metrics(store)

    assert m["prs_opened"] == 1 and m["prs_merged"] == 1
    assert m["attempts_total"] == 2 and m["attempts_per_pr"] == 2.0
    assert m["tokens_per_pr"] == 1650  # (100+50 tokens) + (1000+500 cache)
    prof = {p["profile"]: p for p in m["by_auth_profile"]}
    assert prof["personal"]["attempts"] == 2
    assert m["review_pass"] == 1 and m["review_fail"] == 1
    assert m["repro_gate_verdicts"] == {"waived": 1}
    assert any("off-by-one" in r for r in m["recent_rejection_reasons"])


async def test_ci_gate_counts_come_from_the_events(store):
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [
        _ev("ci_gate_trigger"), _ev("ci_gate_trigger"),
        _ev("ci_gate_pass"), _ev("ci_gate_fail"),
    ])
    m = await compute_metrics(store)
    assert m["ci_gate"] == {"triggered": 2, "passed": 1, "failed": 1}


async def test_ci_gate_block_is_zeroes_on_an_empty_db(store):
    m = await compute_metrics(store)
    assert m["ci_gate"] == {"triggered": 0, "passed": 0, "failed": 0}


async def test_cache_economics_expose_the_creation_share(store):
    """P0.3: a rising cache_creation share means the prompt prefix is being
    rebuilt (full price) instead of reused (~10% price) — the number every
    context change must be judged against."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    a1 = await store.create_attempt(t.id, attempt_number=1)
    a2 = await store.create_attempt(t.id, attempt_number=2)
    await store.update_attempt(a1, cache_creation_tokens=100_000,
                               cache_read_tokens=900_000)
    await store.update_attempt(a2, cache_creation_tokens=300_000,
                               cache_read_tokens=700_000)
    m = await compute_metrics(store)
    ce = m["cache_economics"]
    assert ce["attempts_measured"] == 2
    assert ce["cache_creation_total"] == 400_000
    assert ce["cache_read_total"] == 1_600_000
    assert ce["creation_per_attempt"] == 200_000
    assert ce["creation_share"] == 0.2


async def test_cache_economics_zero_state(store):
    m = await compute_metrics(store)
    assert m["cache_economics"]["attempts_measured"] == 0
    assert m["cache_economics"]["creation_share"] is None


async def test_metrics_expose_the_three_cost_buckets(store):
    """The UI cannot price a burn honestly from ``tokens_per_pr`` alone.

    ``tokens_per_pr`` is ``(tokens_used + cache_read) // prs_OPENED`` — it excludes
    cache-CREATION entirely and divides by opened, not merged. Pricing it as if it were
    fresh tokens (or splitting it by ``creation_share``, whose universe is
    ``creation + read``) is a category error: the Stats page ended up showing two blended
    rates 20% apart on the same screen. Emit the raw buckets so ONE cost function can
    serve every surface and they cannot drift apart.
    """
    t = Task.new("t", repo_path="/r")
    await store.create_task(t)
    a = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        a, tokens_used=150, cache_read_tokens=1500, cache_creation_tokens=90,
        pr_url="https://forge/pr/9",
    )

    m = await compute_metrics(store)

    assert m["tokens_used_total"] == 150          # in + out only
    assert m["cache_economics"]["cache_creation_total"] == 90
    assert m["cache_economics"]["cache_read_total"] == 1500
    assert m["prs_opened"] == 1 and m["prs_merged"] == 0


async def test_reviewer_tokens_are_persisted_and_priced(store):
    """The reviewer's burn was thrown away, so no cost surface could see it.

    ``_run_review`` returns a ReviewDecision carrying tokens_used / cache_read_tokens /
    cache_creation_tokens, and the orchestrator never wrote them down: the DB held the CODER's
    tokens only. The Stats tile is the first surface to make an absolute dollar claim, so it had
    to say "coder spend" — with 59 Opus-4-8 review runs over full diffs costing nothing on the
    record. Persist them in their OWN columns (coder attribution stays intact for by_tier) and
    surface the totals so the UI can price true spend.
    """
    t = Task.new("t", repo_path="/r")
    await store.create_task(t)
    a = await store.create_attempt(t.id, 1)
    await store.update_attempt(
        a, tokens_used=100, cache_read_tokens=1000, cache_creation_tokens=50,
        review_tokens_used=30, review_cache_read_tokens=400, review_cache_creation_tokens=20,
        pr_url="https://forge/pr/1",
    )

    m = await compute_metrics(store)

    # The coder's buckets are untouched...
    assert m["tokens_used_total"] == 100
    assert m["cache_economics"]["cache_creation_total"] == 50
    assert m["cache_economics"]["cache_read_total"] == 1000
    # ...and the reviewer's are now on the record, separately.
    assert m["review_tokens_used_total"] == 30
    assert m["review_cache_creation_total"] == 20
    assert m["review_cache_read_total"] == 400
