"""Tests for the north-star scorecard, gate, and report (Task A4)."""

from __future__ import annotations

from no_human.eval.northstar import BenchScore
from no_human.eval.northstar_card import (
    NorthStarCard,
    northstar_gate,
    render_northstar_md,
)


def _score(*, satisfied=True, nh=500, orig=1000, corrections=3,
           status="awaiting_approval", expected_escalation=False,
           task_id="ns-1") -> BenchScore:
    return BenchScore(
        task_id=task_id, title="t", outcome_status=status,
        goal_satisfied=satisfied, escalated_honestly=False, mergeable=True,
        nh_tokens=nh, nh_cache_tokens=100, nh_cache_creation_tokens=20, nh_turns=5, nh_wall_clock_s=60.0,
        orig_tokens=orig, orig_cache_tokens=0,
        orig_cache_creation_tokens=0, orig_wall_clock_s=600.0,
        orig_corrections=corrections,
        expected_escalation=expected_escalation)


def test_card_aggregates():
    card = NorthStarCard(scores=[
        _score(task_id="a", satisfied=True, nh=500, orig=1000, corrections=3),
        _score(task_id="b", satisfied=False, nh=2000, orig=1000, corrections=5),
        _score(task_id="c", status="skipped", satisfied=None, nh=0),
    ])
    assert card.total == 3 and card.skipped == 1
    assert card.satisfied == 1
    assert card.success_rate == 0.5
    # ratios: 0.5 and 2.0 → median 1.25
    assert card.median_token_ratio == 1.25
    # only satisfied tasks count corrections avoided
    assert card.corrections_avoided == 3


def test_honest_escalation_rate_uses_explicit_field():
    card = NorthStarCard(scores=[
        _score(task_id="a", expected_escalation=True, satisfied=True,
               status="escalated"),
        _score(task_id="b", expected_escalation=True, satisfied=False,
               status="awaiting_approval"),   # faked a PR — not honest
        _score(task_id="c", satisfied=True),
    ])
    assert card.honest_escalation_rate == 0.5
    assert NorthStarCard(scores=[_score()]).honest_escalation_rate == 1.0


def test_gate_first_run_passes_and_becomes_baseline():
    g = northstar_gate(NorthStarCard(scores=[_score()]), None)
    assert g.passed and "baseline" in g.reasons[0]


def test_gate_blocks_success_drop():
    prev = NorthStarCard(scores=[_score(satisfied=True),
                                 _score(task_id="b", satisfied=True)])
    cur = NorthStarCard(scores=[_score(satisfied=True),
                                _score(task_id="b", satisfied=False)])
    g = northstar_gate(cur, prev)
    assert not g.passed and "success rate dropped" in g.reasons[0]


def test_gate_blocks_ratio_regression_but_allows_small_drift():
    prev = NorthStarCard(scores=[_score(nh=500, orig=1000)])      # 0.5
    worse = NorthStarCard(scores=[_score(nh=1100, orig=1000)])    # 1.1 (+0.6)
    drift = NorthStarCard(scores=[_score(nh=700, orig=1000)])     # 0.7 (+0.2)
    assert not northstar_gate(worse, prev).passed
    assert northstar_gate(drift, prev).passed


def test_gate_fails_closed_when_current_ratio_vanishes():
    """Review finding: a run that loses its ratio must block, not silently
    skip the cost check."""
    prev = NorthStarCard(scores=[_score(nh=500, orig=1000)])
    no_ratio = NorthStarCard(scores=[_score(nh=500, orig=0)])   # ratio None
    g = northstar_gate(no_ratio, prev)
    assert not g.passed
    assert any("cannot verify cost" in r for r in g.reasons)


def test_md_hides_non_core_rows():
    """Privacy: generated (non-core) specs appear in aggregates only."""
    card = NorthStarCard(scores=[
        _score(task_id="core-1"),
        _score(task_id="gen-1"),
    ], created_at="2026-07-14")
    card.scores[0].subset = "core"
    md = render_northstar_md(card)
    assert "| core-1 |" in md
    assert "gen-1" not in md
    assert "aggregates only" in md


def test_save_load_roundtrip(tmp_path):
    card = NorthStarCard(scores=[_score(expected_escalation=True)],
                         created_at="2026-07-14", label="baseline")
    p = tmp_path / "latest.json"
    card.save(p)
    loaded = NorthStarCard.load(p)
    assert loaded is not None
    assert loaded.label == "baseline" and loaded.total == 1
    assert loaded.scores[0].expected_escalation is True
    assert loaded.median_token_ratio == card.median_token_ratio
    assert NorthStarCard.load(tmp_path / "missing.json") is None


def test_render_md_has_headline_and_no_numeric_selfscore():
    card = NorthStarCard(scores=[_score()], created_at="2026-07-14",
                         label="baseline")
    card.scores[0].subset = "core"   # only curated rows are rendered
    md = render_northstar_md(card)
    assert "Median token ratio" in md
    assert "corrections avoided" in md.lower()
    assert "| ns-1 |" in md
    assert "/10" not in md
