"""Tests for the north-star scorecard, gate, and report (Task A4)."""

from __future__ import annotations

from no_human.eval.northstar import BenchScore
from no_human.eval.northstar_card import (
    NorthStarCard,
    corpus_shortfall,
    northstar_gate,
    render_northstar_md,
    unmeasured_specs,
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
    # Ten specs, not one: a first run has no baseline to be narrower than, so an
    # absolute floor is the only thing that can stop a slice becoming the
    # reference. The intent — a sound first run passes — is unchanged.
    g = northstar_gate(
        NorthStarCard(scores=[_score(task_id=f"p{i}") for i in range(10)]), None)
    assert g.passed and "baseline" in g.reasons[0]


def test_gate_blocks_success_drop():
    prev = NorthStarCard(scores=[_score(satisfied=True),
                                 _score(task_id="b", satisfied=True)])
    cur = NorthStarCard(scores=[_score(satisfied=True),
                                _score(task_id="b", satisfied=False)])
    g = northstar_gate(cur, prev)
    # Not positional: integrity reasons are prepended, so indexing reasons[0]
    # would make this test load-bearing for ordering it does not care about.
    assert not g.passed
    assert any("success rate dropped" in r for r in g.reasons)


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


def test_gate_blocks_a_broken_corpus_even_with_NO_baseline():
    """Review finding: the coverage check sat behind the `previous is None`
    early return, and `eval/results/northstar/` is gitignored — so `previous`
    is None in every fresh clone and CI checkout. That exempted exactly the
    configuration where a broken corpus is most likely to be published as the
    baseline every later run is measured against.
    """
    broken = NorthStarCard(label="first", scores=[
        _score(task_id="c1", satisfied=True)] + [
        _score(task_id=f"s{i}", status="skipped", satisfied=None, nh=0)
        for i in range(9)])
    g = northstar_gate(broken, None)
    assert not g.passed, g.reasons
    assert any("went unmeasured" in r for r in g.reasons)


def test_gate_first_run_still_passes_when_the_corpus_is_sound():
    """The fix must not turn every first run into a failure."""
    sound = NorthStarCard(label="first",
                          scores=[_score(task_id=f"c{i}") for i in range(10)])
    g = northstar_gate(sound, None)
    assert g.passed and "baseline" in g.reasons[0]


def test_gate_blocks_when_most_of_the_corpus_went_unmeasured():
    """The v15 shape: most specs never resolved, so they left ``ran`` entirely
    and the survivors read BETTER than the baseline. Every metric check passes
    — only corpus integrity catches it.

    ``ran`` is held EQUAL to the baseline so the narrowing rule cannot fire;
    this pins the unmeasured-fraction wiring on its own.
    """
    prev = NorthStarCard(label="baseline", scores=[
        _score(task_id="p1", satisfied=True), _score(task_id="p2", satisfied=False),
        _score(task_id="p3", satisfied=False)])                    # 33% over 3
    cur = NorthStarCard(label="broken", scores=[
        _score(task_id="c1", satisfied=True), _score(task_id="c2", satisfied=True),
        _score(task_id="c3", satisfied=True)] + [                  # 100% over 3
        _score(task_id=f"s{i}", status="skipped", satisfied=None, nh=0)
        for i in range(7)])
    # The trap: on every published headline this run looks like an improvement.
    assert cur.success_rate > prev.success_rate
    assert len(cur.ran) == len(prev.ran)          # narrowing rule cannot fire

    g = northstar_gate(cur, prev)
    assert not g.passed
    assert any("went unmeasured" in r for r in g.reasons)
    assert any("7/10" in r for r in g.reasons), g.reasons   # names the numbers


def test_gate_counts_dead_specs_as_unmeasured_too():
    """A spec that RAN but burned zero tokens measured nothing either — the
    backend died before any model call. Same tolerance, same verdict."""
    prev = NorthStarCard(label="baseline",
                         scores=[_score(task_id=f"p{i}") for i in range(4)])
    cur = NorthStarCard(label="saturated", scores=[
        _score(task_id="c1"), _score(task_id="c2")] + [
        _score(task_id=f"d{i}", nh=0) for i in range(2)])   # ran, zero tokens
    assert len(cur.ran) == len(prev.ran)          # narrowing rule cannot fire
    g = northstar_gate(cur, prev)
    assert not g.passed
    assert any("went unmeasured" in r for r in g.reasons), g.reasons


def test_gate_blocks_a_narrower_run_even_when_fully_measured():
    """Nothing skipped, nothing dead — but half the corpus was never loaded
    (a capped run). A narrower run cannot establish 'no regression'."""
    prev = NorthStarCard(label="baseline",
                         scores=[_score(task_id=f"p{i}") for i in range(6)])
    cur = NorthStarCard(label="slice",
                        scores=[_score(task_id=f"c{i}") for i in range(3)])
    assert unmeasured_specs(cur) == (0, 3)        # unmeasured rule cannot fire
    g = northstar_gate(cur, prev)
    assert not g.passed
    assert any("narrower run" in r for r in g.reasons), g.reasons


def test_narrowing_is_measured_on_ran_not_total():
    """Review finding: mutating `len(current.ran) < len(previous.ran)` to
    `current.total < previous.total` survived the whole suite, because every
    fixture had total == ran on both sides. A corpus that GREW while measuring
    FEWER specs is the case that tells them apart, and it is the shape a
    padded-then-skipped run takes. Skips are held under the coverage ceiling so
    that rule cannot be what fires.
    """
    prev = NorthStarCard(label="baseline",
                         scores=[_score(task_id=f"p{i}") for i in range(10)])
    cur = NorthStarCard(label="wider-but-shallower", scores=[
        _score(task_id=f"c{i}") for i in range(9)] + [
        _score(task_id=f"s{i}", status="skipped", satisfied=None, nh=0)
        for i in range(2)])
    assert cur.total > prev.total            # corpus GREW
    assert len(cur.ran) < len(prev.ran)      # yet measured FEWER
    assert unmeasured_specs(cur) == (2, 11)  # 18% — under the ceiling
    g = northstar_gate(cur, prev)
    assert not g.passed
    assert any("narrower run" in r for r in g.reasons), g.reasons


def test_coverage_ceiling_is_exclusive_at_exactly_twenty_percent():
    """Pins the boundary: `>` not `>=`. Exactly 20% unmeasured is tolerated,
    so a corpus with a couple of known-unrunnable specs is not vetoed."""
    prev = NorthStarCard(label="baseline",
                         scores=[_score(task_id=f"p{i}") for i in range(8)])
    at_limit = NorthStarCard(label="at-limit", scores=[
        _score(task_id=f"c{i}") for i in range(8)] + [
        _score(task_id=f"s{i}", status="skipped", satisfied=None, nh=0)
        for i in range(2)])
    assert unmeasured_specs(at_limit) == (2, 10)      # exactly 20%
    assert northstar_gate(at_limit, prev).passed
    over = NorthStarCard(label="over", scores=[
        _score(task_id=f"c{i}") for i in range(8)] + [
        _score(task_id=f"s{i}", status="skipped", satisfied=None, nh=0)
        for i in range(3)])
    assert unmeasured_specs(over) == (3, 11)          # 27%
    assert not northstar_gate(over, prev).passed


def test_gate_survives_an_empty_card():
    """The `if total and` zero-guard: without it this raises ZeroDivisionError.
    Unreachable from the CLI today, so it had no defender."""
    prev = NorthStarCard(label="baseline", scores=[_score()])
    g = northstar_gate(NorthStarCard(label="empty", scores=[]), prev)
    assert not g.passed          # nothing measured is not "no regression"


def test_gate_still_passes_a_healthy_fully_measured_run():
    """Guard against the fix becoming a blanket blocker: a clean run at the
    same width, with a tolerable skip count, must still pass."""
    prev = NorthStarCard(label="baseline",
                         scores=[_score(task_id=f"p{i}") for i in range(9)])
    cur = NorthStarCard(label="healthy", scores=[
        _score(task_id=f"c{i}") for i in range(9)] + [
        _score(task_id="s1", status="skipped", satisfied=None, nh=0)])
    assert unmeasured_specs(cur) == (1, 10)       # 10% — under the 20% ceiling
    g = northstar_gate(cur, prev)
    assert g.passed, g.reasons


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
    # The label changed deliberately: crediting tasks no_human REFUSED as
    # "avoided" flattered the headline (on v13, 251 of 350 came from escalated
    # tasks the human still has to do). Assert the SPLIT, not just the phrase.
    assert "follow-ups avoided on delivered tasks" in md.lower()
    assert "correctly escalated" in md.lower()
    # The cost median must carry the denominator it is a median over.
    assert "with a recorded original cost" in md.lower()
    assert "orig follow-ups (proxy)" in md   # per-task table too (review)
    assert "| ns-1 |" in md
    assert "/10" not in md


def test_the_delivered_split_publishes_NUMBERS_not_just_labels():
    """The split is the whole point, and only its LABELS were asserted — so the
    report could print 350 under a "DELIVERED" heading, or a delivered count
    larger than the satisfied count, with the suite green. That is a regression
    straight back to the flattery this split removed.

    3 satisfied: 2 delivered (done / awaiting_approval) + 1 correct escalation.
    Follow-ups: 10 + 20 on the delivered pair, 96 on the escalation.
    """
    card = NorthStarCard(label="x", scores=[
        _score(task_id="d1", status="done", satisfied=True, corrections=10),
        _score(task_id="d2", status="awaiting_approval", satisfied=True,
               corrections=20),
        _score(task_id="g1", status="escalated", satisfied=True,
               corrections=96, expected_escalation=True),
        _score(task_id="f1", satisfied=False, corrections=5),
    ])
    assert card.corrections_avoided == 126           # 10 + 20 + 96
    assert card.corrections_avoided_delivered == 30  # 10 + 20 only

    md = render_northstar_md(card)
    assert "avoided on DELIVERED tasks: 30**" in md, md
    assert "a further 96 belong" in md, md
    # The success line must name the delivered count, not the satisfied count.
    assert "of which 2 DELIVERED" in md, md
    assert "1 correctly ESCALATED" in md, md
def test_gate_blocks_a_narrow_first_run_with_no_baseline():
    """Review finding: coverage is a RATIO over what LOADED, so it is blind to
    specs never loaded. Point --specs-dir at the specs that still resolve (or
    delete the dead ones) and coverage reads a perfect 0/N — which is the likely
    REACTION to a coverage refusal, and would have handed the incident run the
    baseline anyway over the same survivors. With no baseline to be narrower
    than, an absolute floor is the only thing that can see it."""
    slice_run = NorthStarCard(label="filtered",
                              scores=[_score(task_id=f"c{i}") for i in range(5)])
    assert unmeasured_specs(slice_run) == (0, 5)   # coverage cannot fire
    g = northstar_gate(slice_run, None)
    assert not g.passed
    assert any("no baseline" in r for r in g.reasons), g.reasons


def test_gate_blocks_an_empty_baseline():
    """Reachable via `--prev <file>`, which accepts any results file. Every
    comparison would silently pass against a baseline that measured nothing."""
    empty_prev = NorthStarCard(label="empty", scores=[])
    cur = NorthStarCard(label="cur",
                        scores=[_score(task_id=f"c{i}") for i in range(10)])
    g = northstar_gate(cur, empty_prev)
    assert not g.passed
    assert any("nothing to compare" in r for r in g.reasons), g.reasons


def test_gate_blocks_a_filtered_slice_of_the_available_corpus():
    """THE incident reaction, and the case a spec-count floor cannot see.

    Filter --specs-dir to the 19 specs that still resolve out of 55: coverage
    reads a perfect 0/19, 19 clears the 10-spec floor comfortably, and on a
    fresh clone there is no baseline to be narrower than. Every other rule
    passes and a 100%-success run becomes the published baseline. Only
    loaded-vs-available sees it.
    """
    survivors = NorthStarCard(
        label="filtered", corpus_available=55,
        scores=[_score(task_id=f"c{i}") for i in range(19)])
    assert unmeasured_specs(survivors) == (0, 19)      # coverage cannot fire
    assert len(survivors.ran) >= 10                    # the floor cannot fire
    g = northstar_gate(survivors, None)
    assert not g.passed, g.reasons
    assert any("19 of 55" in r for r in g.reasons), g.reasons


def test_gate_allows_a_run_that_loaded_the_whole_corpus():
    """The shortfall rule must not fire on an honest full run."""
    full = NorthStarCard(label="full", corpus_available=55,
                         scores=[_score(task_id=f"c{i}") for i in range(55)])
    assert northstar_gate(full, None).passed


def test_the_first_run_floor_counts_ran_not_total():
    """Same ran-vs-total axis a sibling rule was pinned on. total=12 clears a
    naive `total < 10`, but only 8 specs were MEASURED, and the skips sit at
    the coverage ceiling so coverage stays silent."""
    card = NorthStarCard(label="thin", corpus_available=12, scores=[
        _score(task_id=f"c{i}") for i in range(8)] + [
        _score(task_id=f"s{i}", status="skipped", satisfied=None, nh=0)
        for i in range(2)])
    assert card.total == 10 and len(card.ran) == 8
    assert unmeasured_specs(card) == (2, 10)           # 20% — at the ceiling
    g = northstar_gate(card, None)
    assert not g.passed
    assert any("no baseline" in r for r in g.reasons), g.reasons


def test_corpus_available_survives_save_and_load(tmp_path):
    """The whole loaded-vs-available rule depends on this field being produced
    by the CLI and surviving serialisation — and the entire plumbing could be
    severed with all 2021 tests green. `nh bench publish <file>` reconstructs
    the card via `load`, so if the key is ever dropped the filtered-slice
    refusal is silently dead in the real flow."""
    card = NorthStarCard(label="x", corpus_available=55,
                         scores=[_score(task_id=f"c{i}") for i in range(11)])
    p = tmp_path / "run.json"
    card.save(p)
    assert '"corpus_available": 55' in p.read_text()
    reloaded = NorthStarCard.load(p)
    assert reloaded.corpus_available == 55
    # And the rule still fires on the RELOADED card, not just the built one.
    assert "11 of 55" in corpus_shortfall(reloaded)
