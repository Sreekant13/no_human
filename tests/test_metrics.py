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
