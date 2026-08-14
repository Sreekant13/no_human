"""`scripts/measure_cache_burn.py` — the offline before/after instrument for
the coder-in-attempt cache-burn fix (AC1: "measurably reduced ... before/after
numbers in the PR body").

It replays REAL recorded per-turn cache-read growth (from `task_events`)
through a counterfactual compaction model, never a synthetic/generated input
set — so these tests seed a real sqlite DB via `Store.save_events`, the same
write path the product uses, rather than fabricating rows the script has
never actually seen the shape of.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from no_human.core.db import Store

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "measure_cache_burn.py"
_spec = importlib.util.spec_from_file_location("measure_cache_burn", _SCRIPT_PATH)
measure_cache_burn = importlib.util.module_from_spec(_spec)
sys.modules["measure_cache_burn"] = measure_cache_burn
_spec.loader.exec_module(measure_cache_burn)


def _seed(db_path: Path, task_id: str, attempt_events: list[tuple[int, list[dict]]]):
    """`attempt_events`: [(attempt_number, [usage-meta-dict, ...]), ...].

    Emits one `attempt_start` event per attempt (as the orchestrator does)
    followed by its usage events, ts strictly increasing across the whole
    sequence — the same ordering the real attempt loop produces.
    """
    async def _go():
        async with Store(db_path) as s:
            ts = 1_000.0
            events = []
            for attempt_n, rows in attempt_events:
                events.append({
                    "source": "orchestrator", "kind": "attempt_start",
                    "text": f"attempt {attempt_n}/3", "ts": ts,
                })
                ts += 1.0
                for meta in rows:
                    events.append({
                        "source": "agent", "kind": "usage", "ts": ts, **meta,
                    })
                    ts += 1.0
            await s.save_events(task_id, events)
    asyncio.run(_go())


def test_replay_reports_a_reduction_on_a_recorded_stream(tmp_path):
    """A realistic long tail: the coder writes a steady stream of new context
    (`cache_creation_tokens`) turn over turn, as recorded historically (no
    compaction ever fired — the AS-RECORDED baseline this script builds is
    itself a replay, not the raw `cache_read_tokens` column, precisely so it
    is comparable to the windowed replay on identical terms). The windowed
    counterfactual must show a strictly lower total, and the median % must
    match a hand-computed value for a single, simple attempt.
    """
    db = tmp_path / "test.db"
    # A constant 20,000 new tokens written to cache every turn, 20 turns.
    rows = [
        {"cache_read_tokens": 0, "cache_creation_tokens": 20_000,
         "tokens_used": 0}
        for _ in range(20)
    ]
    _seed(db, "task-1", [(1, rows)])

    result = measure_cache_burn.measure(
        db, task_id="task-1", window=140_000, floor_fraction=0.3,
    )

    assert result["attempts_measured"] == 1

    # Hand-computed: growth is a constant 20,000/turn.
    # Window=140,000, floor=42,000 (0.3 * 140,000).
    window, floor = 140_000, int(140_000 * 0.3)
    running, expected_before = 0, 0
    for _ in range(20):
        running += 20_000
        expected_before += running
    running, expected_after, compactions = 0, 0, 0
    for _ in range(20):
        running += 20_000
        if running > window:
            compactions += 1
            running = floor
        expected_after += running

    assert result["before_median_modelled_burn"] == expected_before
    assert result["after_median_modelled_burn"] == expected_after
    assert expected_after < expected_before, \
        "the windowed counterfactual must be strictly lower"
    assert result["simulated_compactions_total"] == compactions
    expected_pct = round((1 - expected_after / expected_before) * 100, 1)
    assert result["median_reduction_pct"] == expected_pct


def test_windowed_replay_never_exceeds_the_unbounded_replay(tmp_path):
    """Regression pin: an earlier version derived growth by diffing the
    (sometimes non-monotonic) `cache_read_tokens` series directly, and some
    real attempts came out with the "after" total HIGHER than "before" — a
    modelling bug. Both series are now built from the identical
    `cache_creation_tokens` growth input, so per-attempt `after <= before`
    always holds, even with volatile/non-monotonic growth."""
    db = tmp_path / "test.db"
    # Deliberately volatile — includes a zero and a big spike, unlike a clean
    # monotonic ramp.
    growths = [50_000, 0, 5_000, 90_000, 0, 0, 40_000, 100_000, 1_000]
    rows = [{"cache_read_tokens": 0, "cache_creation_tokens": g, "tokens_used": 0}
            for g in growths]
    _seed(db, "task-volatile", [(1, rows)])

    result = measure_cache_burn.measure(
        db, task_id="task-volatile", window=140_000, floor_fraction=0.3,
    )
    row = result["per_attempt"][0]
    assert row["after_modelled_burn"] <= row["before_modelled_burn"]


def test_empty_input_set_is_a_FAILURE_not_a_clean_zero(tmp_path):
    # Missing DB file entirely.
    with pytest.raises(SystemExit):
        measure_cache_burn.measure(
            tmp_path / "does-not-exist.db", task_id=None,
            window=140_000, floor_fraction=0.3,
        )

    # A real, empty DB (no matching coder usage events).
    db = tmp_path / "empty.db"

    async def _touch():
        async with Store(db):
            pass
    asyncio.run(_touch())

    with pytest.raises(SystemExit):
        measure_cache_burn.measure(
            db, task_id=None, window=140_000, floor_fraction=0.3,
        )

    # A task_id filter that matches nothing.
    _seed(db, "task-real", [(1, [
        {"cache_read_tokens": 1000, "cache_creation_tokens": 0, "tokens_used": 0},
        {"cache_read_tokens": 2000, "cache_creation_tokens": 0, "tokens_used": 0},
    ])])
    with pytest.raises(SystemExit):
        measure_cache_burn.measure(
            db, task_id="no-such-task", window=140_000, floor_fraction=0.3,
        )


def test_subagent_and_other_role_usage_is_excluded(tmp_path):
    """Only `source == "agent"` (the coder's OWN top-level session, per
    `orchestrator.CODER_ROLE`) usage rows enter the population — a reviewer
    or planner session's usage must never be folded into the coder's burn."""
    db = tmp_path / "test.db"

    async def _go():
        async with Store(db) as s:
            await s.save_events("task-1", [
                {"source": "orchestrator", "kind": "attempt_start",
                 "text": "attempt 1/3", "ts": 1.0},
                {"source": "agent", "kind": "usage", "ts": 2.0,
                 "cache_read_tokens": 0, "cache_creation_tokens": 1000,
                 "tokens_used": 0},
                {"source": "agent", "kind": "usage", "ts": 3.0,
                 "cache_read_tokens": 0, "cache_creation_tokens": 2000,
                 "tokens_used": 0},
                # A reviewer session's usage, interleaved by timestamp —
                # must NOT be counted as coder burn.
                {"source": "reviewer", "kind": "usage", "ts": 2.5,
                 "cache_read_tokens": 0, "cache_creation_tokens": 999_999,
                 "tokens_used": 0},
            ])
    asyncio.run(_go())

    result = measure_cache_burn.measure(
        db, task_id="task-1", window=140_000, floor_fraction=0.3,
    )
    assert result["attempts_measured"] == 1
    # running after turn 1 = 1000, after turn 2 = 3000; sum = 4000.
    assert result["before_median_modelled_burn"] == 4000, (
        "the reviewer's 999,999 must not appear in the coder's total"
    )


def test_role_other_than_coder_is_refused(tmp_path, capsys):
    db = tmp_path / "test.db"
    rc = measure_cache_burn.main(["--db", str(db), "--role", "reviewer"])
    assert rc != 0
    assert "role must be 'coder'" in capsys.readouterr().err
