"""Paired per-spec bench comparison (bench-v2 · V2).

Every results dict here is built IN-TEST from placeholder specs (proj-a, …).
Nothing loads a real run: a fixture copied out of `eval/results/` would carry
real project labels and note text into a shipped test file, and the properties
under test are structural, so a real corpus adds risk and no signal.
"""

from __future__ import annotations

import math

import pytest

from no_human.eval.bench_compare import (
    MIN_DISCORDANT_FOR_POWER,
    REQUIRED_SCORE_KEYS,
    ResultsSchemaError,
    compare_runs,
    flaky_canary,
    interpretation,
    mcnemar_exact_p,
    order_runs,
    spec_verdicts,
    trial_flip_count,
    undated_run_indices,
    validate_results,
)
from no_human.eval.northstar import BenchScore
from no_human.eval.northstar_card import (
    NorthStarCard,
    score_ran,
    score_succeeded,
)


def _row(task_id: str, *, ok: bool | None = True, trial: int = 0,
         status: str = "awaiting_approval", notes: str = "",
         title: str = "", expected_escalation: bool = False,
         escalated_honestly: bool = False) -> dict:
    """One score row in the on-disk shape (`BenchScore.as_dict()`'s keys)."""
    return {
        "task_id": task_id, "title": title or f"task {task_id}",
        "outcome_status": status, "goal_satisfied": ok,
        "escalated_honestly": escalated_honestly, "mergeable": ok,
        "expected_escalation": expected_escalation,
        "subset": "core", "project": "proj-a", "trial": trial,
        "notes": notes, "events": [],
    }


def _run(label: str, rows: list[dict], created_at: str = "2026-01-01T00:00:00") -> dict:
    return {"created_at": created_at, "label": label, "aggregate": {},
            "scores": rows}


# --------------------------------------------------------------------------- #
# verdicts
# --------------------------------------------------------------------------- #

def test_single_trial_verdict_is_the_row_itself():
    verdicts, unmeasured = spec_verdicts(
        _run("a", [_row("s1", ok=True), _row("s2", ok=False)]))
    assert unmeasured == []
    assert verdicts["s1"].success is True and verdicts["s1"].trials == 1
    assert verdicts["s2"].success is False


def test_multi_trial_verdict_is_a_majority_vote():
    rows = [_row("s1", ok=True, trial=0), _row("s1", ok=True, trial=1),
            _row("s1", ok=False, trial=2)]
    verdicts, _ = spec_verdicts(_run("a", rows))
    assert verdicts["s1"].passes == 2 and verdicts["s1"].trials == 3
    assert verdicts["s1"].success is True
    assert verdicts["s1"].pass_fraction == pytest.approx(2 / 3)


def test_a_tied_multi_trial_spec_is_not_a_success():
    """2/4 is a coin flip. Rounding a tie up would let a spec that flips half
    its trials pair as a clean success against a baseline that did it every
    time — the exact regression this module exists to surface."""
    rows = [_row("s1", ok=i < 2, trial=i) for i in range(4)]
    verdicts, _ = spec_verdicts(_run("a", rows))
    assert verdicts["s1"].passes == 2 and verdicts["s1"].trials == 4
    assert verdicts["s1"].success is False


def test_a_spec_skipped_in_every_trial_is_unmeasured_not_failed():
    verdicts, unmeasured = spec_verdicts(_run("a", [
        _row("s1", ok=True),
        _row("s2", ok=None, status="skipped", trial=0),
        _row("s2", ok=None, status="skipped", trial=1),
    ]))
    assert unmeasured == ["s2"] and "s2" not in verdicts
    assert verdicts["s1"].success is True


def test_a_partly_skipped_spec_is_judged_on_the_trials_that_ran():
    verdicts, unmeasured = spec_verdicts(_run("a", [
        _row("s1", ok=None, status="skipped", trial=0),
        _row("s1", ok=True, trial=1),
    ]))
    assert unmeasured == []
    assert verdicts["s1"].trials == 1 and verdicts["s1"].success is True


# --------------------------------------------------------------------------- #
# pairing
# --------------------------------------------------------------------------- #

def test_identical_runs_have_no_flips_and_p_is_one():
    rows = [_row("s1", ok=True), _row("s2", ok=False), _row("s3", ok=True)]
    cmp = compare_runs(_run("base", rows), _run("same", list(rows)))
    assert cmp.regressions == [] and cmp.fixes == []
    assert (cmp.both_pass, cmp.both_fail) == (2, 1)
    assert cmp.discordant == 0
    assert cmp.p_value == 1.0
    assert cmp.paired == 3 and cmp.unpaired == 0


def test_flip_directions_are_labelled_from_the_baseline():
    a = _run("base", [_row("s1", ok=True), _row("s2", ok=False),
                      _row("s3", ok=True)])
    b = _run("change", [_row("s1", ok=False, status="escalated",
                             notes="ran out of budget"),
                        _row("s2", ok=True), _row("s3", ok=True)])
    cmp = compare_runs(a, b)
    assert cmp.b_regressed == 1 and cmp.c_fixed == 1
    assert [f.task_id for f in cmp.regressions] == ["s1"]
    assert [f.task_id for f in cmp.fixes] == ["s2"]
    reg = cmp.regressions[0]
    assert reg.direction == "regressed"
    assert reg.a.outcome_status == "awaiting_approval"
    assert reg.b.outcome_status == "escalated"
    assert reg.b.notes == "ran out of budget"
    assert cmp.both_pass == 1 and cmp.both_fail == 0
    # b == c == 1 → the doubled tail is exactly 1.0, never above it.
    assert cmp.p_value == 1.0


def test_a_only_and_b_only_specs_are_named_never_dropped():
    a = _run("base", [_row("s1", ok=True), _row("gone", ok=True)])
    b = _run("change", [_row("s1", ok=True), _row("new", ok=False)])
    cmp = compare_runs(a, b)
    assert cmp.only_in_a == ["gone"] and cmp.only_in_b == ["new"]
    assert cmp.paired == 1
    assert cmp.unpaired == 2
    # And the unpaired specs are NOT quietly folded into a concordant cell.
    assert cmp.both_pass + cmp.both_fail + cmp.discordant == 1


def test_specs_unmeasured_on_one_side_are_reported_not_scored_as_flips():
    a = _run("base", [_row("s1", ok=True), _row("s2", ok=True)])
    b = _run("change", [_row("s1", ok=True),
                        _row("s2", ok=None, status="skipped")])
    cmp = compare_runs(a, b)
    assert cmp.unmeasured_b == ["s2"] and cmp.unmeasured_a == []
    assert cmp.b_regressed == 0, "a skip is not a regression"
    assert cmp.only_in_a == ["s2"], "and it is still named as unpaired"
    # ONE spec is missing from the pairing, not two: the same s2 is both
    # `only_in_a` and `unmeasured_b`, and summing the lists would report the
    # gap as twice its real size.
    assert cmp.unpaired == 1


def test_rates_are_over_each_runs_own_measured_specs():
    a = _run("base", [_row("s1", ok=True), _row("s2", ok=False)])
    b = _run("change", [_row("s1", ok=True), _row("s2", ok=True),
                        _row("s3", ok=None, status="skipped")])
    cmp = compare_runs(a, b)
    assert cmp.specs_a == 2 and cmp.rate_a == 0.5
    assert cmp.specs_b == 2 and cmp.rate_b == 1.0


def test_per_trial_flip_counts_are_reported_alongside_the_verdict():
    """The verdict moved by one vote; three of four trials still agreed. A
    spec that was always a coin flip and a spec that genuinely broke produce
    the same verdict flip and very different trial counts."""
    a = _run("base", [_row("s1", ok=i < 3, trial=i) for i in range(4)])
    b = _run("change", [_row("s1", ok=i < 2, trial=i) for i in range(4)])
    cmp = compare_runs(a, b)
    assert cmp.b_regressed == 1
    flip = cmp.regressions[0]
    assert (flip.a.passes, flip.b.passes) == (3, 2)
    assert (flip.trial_flips, flip.trials_paired) == (1, 4)


def test_trial_flip_count_pairs_on_trial_index_only():
    a = [_row("s1", ok=True, trial=0), _row("s1", ok=True, trial=1)]
    b = [_row("s1", ok=False, trial=0), _row("s1", ok=True, trial=1),
         _row("s1", ok=False, trial=2)]
    assert trial_flip_count(a, b) == (1, 2)


# --------------------------------------------------------------------------- #
# McNemar
# --------------------------------------------------------------------------- #

def test_mcnemar_b_plus_c_zero_is_one():
    assert mcnemar_exact_p(0, 0) == 1.0


def test_mcnemar_is_the_exact_two_sided_binomial():
    # 5 regressions, 0 fixes: 2 * C(5,0) / 2^5
    assert mcnemar_exact_p(5, 0) == pytest.approx(2 * 1 / 32)
    # 4 and 1: 2 * (C(5,0) + C(5,1)) / 2^5
    assert mcnemar_exact_p(4, 1) == pytest.approx(2 * (1 + 5) / 32)
    # symmetric in its arguments — direction does not change the p
    assert mcnemar_exact_p(1, 4) == mcnemar_exact_p(4, 1)


def test_mcnemar_never_exceeds_one():
    for n in range(1, 12):
        for b in range(n + 1):
            p = mcnemar_exact_p(b, n - b)
            assert 0.0 < p <= 1.0, (b, n - b, p)


def test_mcnemar_matches_a_brute_force_tail():
    for b, c in ((3, 1), (7, 2), (6, 6), (9, 0)):
        n = b + c
        expect = min(1.0, 2.0 * sum(math.comb(n, i)
                                    for i in range(min(b, c) + 1)) / 2 ** n)
        assert mcnemar_exact_p(b, c) == pytest.approx(expect)


def test_mcnemar_rejects_negative_counts():
    with pytest.raises(ValueError):
        mcnemar_exact_p(-1, 2)


def test_the_power_floor_is_where_significance_first_becomes_reachable():
    """The floor is not a taste call: at n discordant pairs the SMALLEST
    attainable two-sided p is 2/2^n, so below the floor no split of the pairs
    can reach 0.05 and the p carries no information at all."""
    below = MIN_DISCORDANT_FOR_POWER - 1
    assert min(mcnemar_exact_p(b, below - b)
               for b in range(below + 1)) > 0.05
    assert min(mcnemar_exact_p(b, MIN_DISCORDANT_FOR_POWER - b)
               for b in range(MIN_DISCORDANT_FOR_POWER + 1)) <= 0.05


def test_interpretation_says_so_when_the_test_has_no_power():
    a = _run("base", [_row("s1", ok=True), _row("s2", ok=True)])
    b = _run("change", [_row("s1", ok=False), _row("s2", ok=True)])
    cmp = compare_runs(a, b)
    assert cmp.discordant == 1 and cmp.has_power is False
    text = interpretation(cmp)
    assert "CANNOT reach p<0.05" in text and "1 discordant" in text

    same = compare_runs(a, _run("same", [_row("s1", ok=True),
                                         _row("s2", ok=True)]))
    assert "nothing for a test to weigh" in interpretation(same)


# --------------------------------------------------------------------------- #
# flaky canary
# --------------------------------------------------------------------------- #

def _hist(*verdicts: bool, task_id: str = "s1") -> list[dict]:
    return [_run(f"r{i}", [_row(task_id, ok=v)],
                 created_at=f"2026-01-0{i + 1}T00:00:00")
            for i, v in enumerate(verdicts)]


def test_canary_needs_two_flips_not_one():
    once = flaky_canary(_hist(True, False, False))
    assert once == [], "one flip is a regression, not a flake"
    twice = flaky_canary(_hist(True, False, True))
    assert [c.task_id for c in twice] == ["s1"]
    assert twice[0].flips == 2 and twice[0].pairs == 2
    assert twice[0].history == ["✓", "✗", "✓"]


def test_canary_needs_at_least_two_runs():
    assert flaky_canary([]) == []
    assert flaky_canary(_hist(True)) == []


def test_canary_orders_by_created_at_not_by_argument_order():
    """A caller that globs a directory gets filesystem order; a canary computed
    over shuffled history is noise measuring itself."""
    runs = _hist(True, False, True)
    shuffled = [runs[2], runs[0], runs[1]]
    assert [c.flips for c in flaky_canary(shuffled)] == [2]
    # The same three verdicts in true chronological order ✓✗✓ flip twice; had
    # the shuffle been honoured (✓✓✗) they would flip once and drop out.
    assert flaky_canary([runs[0], runs[2], runs[1]])[0].history == ["✓", "✗", "✓"]


def test_a_spec_absent_from_a_run_breaks_the_chain_rather_than_flipping():
    runs = [
        _run("r1", [_row("s1", ok=True)], created_at="2026-01-01T00:00:00"),
        _run("r2", [_row("other", ok=True)], created_at="2026-01-02T00:00:00"),
        _run("r3", [_row("s1", ok=False)], created_at="2026-01-03T00:00:00"),
    ]
    assert flaky_canary(runs) == [], (
        "a missing repo is not a flip, and the runs either side of a gap are "
        "not consecutive observations of the spec")


def test_canary_counts_only_the_flipping_spec():
    runs = [
        _run("r1", [_row("flip", ok=True), _row("steady", ok=True)],
             created_at="2026-01-01T00:00:00"),
        _run("r2", [_row("flip", ok=False), _row("steady", ok=True)],
             created_at="2026-01-02T00:00:00"),
        _run("r3", [_row("flip", ok=True), _row("steady", ok=True)],
             created_at="2026-01-03T00:00:00"),
    ]
    assert [c.task_id for c in flaky_canary(runs)] == ["flip"]


def test_canary_min_flips_is_tunable_but_defaults_to_two():
    assert [c.task_id for c in flaky_canary(_hist(True, False, False),
                                            min_flips=1)] == ["s1"]


# --------------------------------------------------------------------------- #
# ordering — an undated run must not be promoted to the front (review F1)
# --------------------------------------------------------------------------- #

def test_an_undated_run_sorts_LAST_not_first():
    """`str(created_at or "")` sorts "" BEFORE every real timestamp, so an
    undated run used to be silently promoted to the FRONT of the history
    whatever position the caller supplied it in."""
    dated = _run("r1", [_row("s1", ok=True)], created_at="2026-01-01T00:00:00")
    undated = _run("rX", [_row("s1", ok=False)], created_at="")
    assert [r["label"] for r in order_runs([dated, undated])] == ["r1", "rX"]
    assert [r["label"] for r in order_runs([undated, dated])] == ["r1", "rX"]


def test_undated_runs_keep_their_supplied_order_among_themselves():
    a = _run("a", [_row("s1", ok=True)], created_at="")
    b = _run("b", [_row("s1", ok=True)], created_at="")
    mid = _run("m", [_row("s1", ok=True)], created_at="2026-01-01T00:00:00")
    assert [r["label"] for r in order_runs([b, mid, a])] == ["m", "b", "a"]


def test_undated_run_indices_names_them_in_supplied_order():
    runs = [_run("a", [_row("s1")], created_at="2026-01-01T00:00:00"),
            _run("b", [_row("s1")], created_at=""),
            _run("c", [_row("s1")], created_at="2026-01-02T00:00:00"),
            _run("d", [_row("s1")], created_at="")]
    assert undated_run_indices(runs) == [1, 3]
    assert undated_run_indices(runs[:1]) == []


def test_an_undated_fourth_run_supplied_last_does_not_change_the_verdict():
    """The review's verdict-changing case, both ways round.

    History ✓ ✗ ✓ flips twice and is flagged. Append a fourth run that agrees
    with the third: dated, it extends the chain and the spec stays flagged at
    2 flips. UNDATED and supplied last, it must land in the same place — the
    old key put it FIRST, which made the chain ✓ ✓ ✗ ✓ ... and moved the
    counts. The two must agree, or a missing date silently re-times history.
    """
    base = [
        _run("r1", [_row("s1", ok=True)], created_at="2026-01-01T00:00:00"),
        _run("r2", [_row("s1", ok=False)], created_at="2026-01-02T00:00:00"),
        _run("r3", [_row("s1", ok=True)], created_at="2026-01-03T00:00:00"),
    ]
    dated_4th = _run("r4", [_row("s1", ok=False)],
                     created_at="2026-01-04T00:00:00")
    undated_4th = _run("r4", [_row("s1", ok=False)], created_at="")

    with_dated = flaky_canary(base + [dated_4th])
    with_undated = flaky_canary(base + [undated_4th])
    assert [(c.task_id, c.flips, c.pairs, c.history) for c in with_dated] == \
           [(c.task_id, c.flips, c.pairs, c.history) for c in with_undated]
    assert with_undated[0].history == ["✓", "✗", "✓", "✗"]
    assert with_undated[0].flips == 3


def test_an_undated_run_no_longer_flips_a_spec_into_the_canary():
    """The other half of the review's demonstration: flagged-vs-not must not
    turn on whether one run carries a date. ✓ ✗ ✗ flips once (not flagged);
    prepending an undated ✗ leaves it at once, where the old key would have
    made the chain ✗ ✓ ✗ ✗ — two flips, flagged."""
    dated = [
        _run("r1", [_row("s1", ok=True)], created_at="2026-01-01T00:00:00"),
        _run("r2", [_row("s1", ok=False)], created_at="2026-01-02T00:00:00"),
        _run("r3", [_row("s1", ok=False)], created_at="2026-01-03T00:00:00"),
    ]
    undated_fail = _run("rX", [_row("s1", ok=False)], created_at="")
    assert flaky_canary(dated) == []
    assert flaky_canary([undated_fail] + dated) == []
    assert flaky_canary(dated + [undated_fail]) == []


# --------------------------------------------------------------------------- #
# shape validation — refuse a drifted file, never render one (review F2)
# --------------------------------------------------------------------------- #

def test_validate_accepts_a_well_formed_run():
    validate_results(_run("ok", [_row("s1", ok=True),
                                 _row("s2", ok=None, status="skipped")]))


def test_validate_refuses_rows_that_carry_only_task_id_and_title():
    """The reviewer's case. Such rows count as RAN (the skip test is an
    inequality) and as FAILED (`bool(None)`), so the file renders a confident
    wall of regressions that reads exactly like a real catastrophe."""
    drifted = _run("drift", [{"task_id": "s1", "title": "t"},
                             {"task_id": "s2", "title": "t"}])
    with pytest.raises(ResultsSchemaError) as exc:
        validate_results(drifted, source="drift.json")
    msg = str(exc.value)
    assert "drift.json" in msg and "2 of 2" in msg
    assert "outcome_status" in msg and "goal_satisfied" in msg
    # And it says WHICH rows, not just how many.
    assert "s1" in msg


def test_validate_refuses_a_dict_with_no_scores_key():
    """The other reviewer case: two of these compare to '0.0% of 0 measured
    spec(s)', zero flips, p=1.0 — a green report over no data at all."""
    for empty in ({"created_at": "2026-01-01", "label": "x"},
                  _run("x", [])):
        with pytest.raises(ResultsSchemaError):
            validate_results(empty, source="empty.json")


def test_the_unvalidated_empty_pair_really_did_render_clean():
    """Non-vacuity for the refusal above: the comparison itself is pure and
    total, so it STILL produces this. The refusal is the only thing standing
    between a drifted file and a confident report — if this assertion ever
    fails, the guard above is no longer the thing protecting anyone."""
    cmp = compare_runs({"label": "a"}, {"label": "b"})
    assert cmp.paired == 0 and cmp.p_value == 1.0 and cmp.rate_a == 0.0


def test_validate_refuses_a_partially_drifted_file_rather_than_skipping_rows():
    """One bad row condemns the file. Skipping it would silently compare a
    spec set nobody chose — the same 'filtered slice stands for the corpus'
    failure the publish gate exists to stop."""
    mixed = _run("mixed", [_row("s1", ok=True), {"task_id": "s2"}])
    with pytest.raises(ResultsSchemaError) as exc:
        validate_results(mixed, source="mixed.json")
    assert "1 of 2" in str(exc.value)


def test_validate_checks_key_PRESENCE_not_truthiness():
    """`goal_satisfied: None` (judge skipped) and `outcome_status: "skipped"`
    are both legitimate on a real card; a truthiness check would refuse every
    run that contains a skip."""
    validate_results(_run("ok", [_row("s1", ok=None, status="skipped")]))
    assert set(REQUIRED_SCORE_KEYS) == {"task_id", "outcome_status",
                                        "goal_satisfied"}


def test_validate_refuses_non_objects():
    with pytest.raises(ResultsSchemaError):
        validate_results(["not", "a", "card"], source="list.json")
    with pytest.raises(ResultsSchemaError):
        validate_results({"scores": "nope"}, source="str.json")
    with pytest.raises(ResultsSchemaError):
        validate_results(_run("x", [None]), source="none-row.json")


# --------------------------------------------------------------------------- #
# the shared predicate — the two paths must never drift
# --------------------------------------------------------------------------- #

def _bench_score(**kw) -> BenchScore:
    base = dict(
        task_id="s1", title="t", outcome_status="escalated",
        goal_satisfied=True, escalated_honestly=True, mergeable=None,
        nh_tokens=10, nh_cache_tokens=0, nh_cache_creation_tokens=0,
        nh_turns=1, nh_wall_clock_s=1.0, orig_tokens=100,
        orig_cache_tokens=0, orig_cache_creation_tokens=0,
        orig_wall_clock_s=10.0, orig_corrections=1,
        expected_escalation=True, subset="core", project="proj-a")
    base.update(kw)
    return BenchScore(**base)


def test_an_honestly_escalated_gated_spec_is_a_success_on_BOTH_paths():
    """The one row where a second, hand-written predicate would diverge.

    The runner sets `goal_satisfied = escalated_honestly` for an
    expect-escalation spec BEFORE the score is written, so the card counts it
    satisfied. `compare_runs` reads the same row out of the JSON. Both go
    through `score_succeeded`, and this asserts they agree on the SAME row
    rather than agreeing with two copies of the rule.
    """
    score = _bench_score()
    card = NorthStarCard(scores=[score])
    assert card.satisfied == 1 and card.success_rate == 1.0

    verdicts, _ = spec_verdicts(_run("a", [score.as_dict()]))
    assert verdicts["s1"].success is True
    assert score_succeeded(score) is score_succeeded(score.as_dict()) is True


def test_a_dishonest_gated_spec_fails_on_BOTH_paths():
    score = _bench_score(goal_satisfied=False, escalated_honestly=False,
                         outcome_status="awaiting_approval")
    card = NorthStarCard(scores=[score])
    assert card.satisfied == 0
    verdicts, _ = spec_verdicts(_run("a", [score.as_dict()]))
    assert verdicts["s1"].success is False


def test_the_predicate_reads_a_BenchScore_and_a_dict_identically():
    """Non-vacuity for the two tests above: the shared functions must give the
    same answer for every field combination that can appear on disk, or one of
    the two callers is silently running a different rule."""
    for ok in (True, False, None):
        for status in ("awaiting_approval", "escalated", "skipped"):
            score = _bench_score(goal_satisfied=ok, outcome_status=status)
            as_dict = score.as_dict()
            assert score_succeeded(score) == score_succeeded(as_dict)
            assert score_ran(score) == score_ran(as_dict)


def test_the_card_and_the_comparison_agree_on_a_whole_mixed_run():
    """End-to-end non-drift: the specs the card counts satisfied are exactly
    the specs the comparison gives a passing verdict, on one single-trial run."""
    scores = [
        _bench_score(task_id="s1", goal_satisfied=True),
        _bench_score(task_id="s2", goal_satisfied=False,
                     escalated_honestly=False),
        _bench_score(task_id="s3", goal_satisfied=None,
                     outcome_status="skipped"),
        _bench_score(task_id="s4", goal_satisfied=True,
                     expected_escalation=False, outcome_status="done"),
    ]
    card = NorthStarCard(scores=scores)
    verdicts, unmeasured = spec_verdicts(
        _run("a", [s.as_dict() for s in scores]))
    assert {v.task_id for v in verdicts.values() if v.success} == {
        s.task_id for s in card.ran if s.goal_satisfied}
    assert card.satisfied == sum(1 for v in verdicts.values() if v.success)
    assert unmeasured == ["s3"] and card.skipped == 1
