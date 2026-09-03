"""Paired per-spec bench comparison (bench-v2 · V2).

Every results dict here is built IN-TEST from placeholder specs (proj-a, …).
Nothing loads a real run: a fixture copied out of `eval/results/` would carry
real project labels and note text into a shipped test file, and the properties
under test are structural, so a real corpus adds risk and no signal.
"""

from __future__ import annotations

import json
import math
import re

import pytest
from click.testing import CliRunner

from no_human.cli.commands import bench_compare, cli
from no_human.eval.bench_compare import (
    COST_TOKEN_KEYS,
    DEFAULT_COST_FLIP_RATIO,
    DEFAULT_COST_TOP,
    MIN_DISCORDANT_FOR_POWER,
    REQUIRED_SCORE_KEYS,
    CostDelta,
    ResultsSchemaError,
    SpecCost,
    compare_runs,
    cost_caveat,
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
         escalated_honestly: bool = False,
         nh_tokens: int | None = None, nh_cache_tokens: int = 0,
         nh_cache_creation_tokens: int = 0,
         cost_ratio: float | None = None,
         nh_role_tokens: dict | None = None) -> dict:
    """One score row in the on-disk shape (`BenchScore.as_dict()`'s keys).

    Cost fields are opt-in and independent of each other, matching a real
    results file: `nh_tokens`/`nh_cache_tokens`/`nh_cache_creation_tokens`
    only appear together when `nh_tokens` is given (a row that never priced
    at all carries none of the three, not zeros); `cost_ratio` and
    `nh_role_tokens` are each emitted only when explicitly passed, so a test
    can build a row with a ratio but no token breakdown, or vice versa —
    exactly the partial-data shapes `_row_cost` has to tolerate.
    """
    row = {
        "task_id": task_id, "title": title or f"task {task_id}",
        "outcome_status": status, "goal_satisfied": ok,
        "escalated_honestly": escalated_honestly, "mergeable": ok,
        "expected_escalation": expected_escalation,
        "subset": "core", "project": "proj-a", "trial": trial,
        "notes": notes, "events": [],
    }
    if nh_tokens is not None:
        row["nh_tokens"] = nh_tokens
        row["nh_cache_tokens"] = nh_cache_tokens
        row["nh_cache_creation_tokens"] = nh_cache_creation_tokens
    if cost_ratio is not None:
        row["cost_ratio"] = cost_ratio
    if nh_role_tokens is not None:
        row["nh_role_tokens"] = nh_role_tokens
    return row


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


# --------------------------------------------------------------------------- #
# per-spec cost — read, never invented
# --------------------------------------------------------------------------- #

def test_spec_verdict_carries_the_costs_of_the_trials_it_voted_on():
    rows = [_row("s1", ok=True, nh_tokens=100, cost_ratio=0.1)]
    verdicts, _ = spec_verdicts(_run("a", rows))
    cost = verdicts["s1"].cost
    assert isinstance(cost, SpecCost)
    assert cost.nh_tokens == 100
    assert cost.cost_ratio == pytest.approx(0.1)
    assert cost.priced_tokens == pytest.approx(100.0)
    assert cost.trials_with_cost == 1


def test_a_row_with_no_cost_fields_has_no_cost_never_zero():
    """A row this module never priced (no `nh_tokens`/cache columns at all)
    must render every cost field absent — `None` — not a fabricated `0.0`
    that would silently read as "this spec cost nothing"."""
    rows = [_row("s1", ok=True)]
    verdicts, _ = spec_verdicts(_run("a", rows))
    cost = verdicts["s1"].cost
    assert cost is not None, "the spec still gets a SpecCost — just an empty one"
    assert cost.priced_tokens is None
    assert cost.nh_tokens is None
    assert cost.cost_ratio is None
    assert cost.trials_with_cost == 0


def test_cost_fields_do_not_become_required_schema():
    """Cost is optional data layered on top of the pass/fail schema — a run
    with none of `COST_TOKEN_KEYS` must still validate and compare cleanly,
    the same as any older results file predating this feature."""
    validate_results(_run("ok", [_row("s1", ok=True)]))
    assert not set(COST_TOKEN_KEYS) & set(REQUIRED_SCORE_KEYS)


def test_top_cost_deltas_name_the_planted_regression():
    """The motivating case: a spec whose cost ratio moved 0.107 -> 0.336 must
    be findable in minutes, not thrown away as a success bit."""
    a = _run("base", [_row("s1", ok=True, nh_tokens=100, cost_ratio=0.107),
                      _row("s2", ok=True, nh_tokens=50, cost_ratio=0.2)])
    b = _run("change", [_row("s1", ok=False, nh_tokens=340, cost_ratio=0.336),
                        _row("s2", ok=True, nh_tokens=50, cost_ratio=0.2)])
    cmp = compare_runs(a, b)

    top = cmp.top_cost_deltas(1)
    assert len(top) == 1 and top[0].task_id == "s1"
    assert top[0].token_delta == pytest.approx(240.0)
    assert top[0].abs_token_delta == pytest.approx(240.0)

    # SUM, not average or median: the unrelated, unmoved s2 does not dilute it.
    assert cmp.aggregate_token_delta == pytest.approx(240.0)
    assert cmp.specs_costed == 2
    assert cmp.specs_missing_cost == 0

    # `top_cost_deltas` never hides data: n<=0 returns every paired spec.
    assert len(cmp.top_cost_deltas(0)) == 2
    assert {d.task_id for d in cmp.top_cost_deltas(0)} == {"s1", "s2"}


def test_a_spec_that_flipped_and_moved_cost_is_flagged():
    a = _run("base", [_row("s1", ok=True, nh_tokens=100, cost_ratio=0.107)])
    b = _run("change", [_row("s1", ok=False, nh_tokens=340, cost_ratio=0.336)])
    cmp = compare_runs(a, b)

    flagged = cmp.cost_flagged(DEFAULT_COST_FLIP_RATIO)
    assert [d.task_id for d in flagged] == ["s1"]
    assert flagged[0].movement_ratio == pytest.approx(3.4)

    # A threshold the movement does not clear leaves it unflagged.
    assert cmp.cost_flagged(10.0) == []


def test_a_flip_with_no_cost_data_is_not_flagged():
    """A spec that flipped but never carried cost data must not be flagged —
    there is nothing to compare it against, and flagging it would fabricate
    a movement out of an absence."""
    a = _run("base", [_row("s1", ok=True)])
    b = _run("change", [_row("s1", ok=False)])
    cmp = compare_runs(a, b)
    assert cmp.cost_flagged(DEFAULT_COST_FLIP_RATIO) == []
    d = cmp.cost_deltas[0]
    assert d.flipped is True
    assert d.movement_ratio is None and d.token_delta is None


def test_movement_ratio_is_none_when_the_baseline_cost_is_zero():
    """Never divide toward infinity: a spec whose baseline genuinely cost
    nothing gets `movement_ratio = None`, not a huge or infinite number."""
    a = _run("base", [_row("s1", ok=True, nh_tokens=0, cost_ratio=0.0)])
    b = _run("change", [_row("s1", ok=True, nh_tokens=50, cost_ratio=0.1)])
    cmp = compare_runs(a, b)
    d = cmp.cost_deltas[0]
    assert d.a.priced_tokens == pytest.approx(0.0)
    assert d.movement_ratio is None
    # token_delta is still reportable even when the ratio is not.
    assert d.token_delta == pytest.approx(50.0)


def test_cost_deltas_do_not_move_any_verdict():
    """Cost is a pure reader over the pairing this module already computes —
    planting a large cost swing on a spec that did not flip must not change
    b_regressed/c_fixed/both_pass/both_fail."""
    a = _run("base", [_row("s1", ok=True, nh_tokens=100, cost_ratio=0.1),
                      _row("s2", ok=False)])
    b = _run("change", [_row("s1", ok=True, nh_tokens=500, cost_ratio=0.9),
                        _row("s2", ok=False)])
    cmp = compare_runs(a, b)
    assert cmp.b_regressed == 0 and cmp.c_fixed == 0
    assert cmp.both_pass == 1 and cmp.both_fail == 1
    assert cmp.regressions == [] and cmp.fixes == []
    # The cost movement is still visible in cost_deltas, just not a flip.
    s1_delta = next(d for d in cmp.cost_deltas if d.task_id == "s1")
    assert s1_delta.flipped is False
    assert s1_delta.token_delta == pytest.approx(400.0)


def test_a_flip_carries_its_own_cost_delta_object():
    a = _run("base", [_row("s1", ok=True, nh_tokens=100, cost_ratio=0.1)])
    b = _run("change", [_row("s1", ok=False, nh_tokens=200, cost_ratio=0.2)])
    cmp = compare_runs(a, b)
    flip = cmp.regressions[0]
    assert isinstance(flip.cost, CostDelta)
    assert flip.cost.task_id == "s1"
    assert flip.cost.token_delta == pytest.approx(100.0)


def test_cost_caveat_names_the_absence_rule():
    text = cost_caveat()
    assert "no delta" in text or "never a fabricated zero" in text


def test_bench_compare_prints_the_cost_section(tmp_path):
    a = _run("base", [_row("s1", ok=True, nh_tokens=100, cost_ratio=0.107),
                      _row("s2", ok=True)],
             created_at="2026-08-29T00:00:00")
    b = _run("change", [_row("s1", ok=False, nh_tokens=340, cost_ratio=0.336),
                        _row("s2", ok=True)],
             created_at="2026-08-30T00:00:00")
    path_a = tmp_path / "run_a.json"
    path_b = tmp_path / "run_b.json"
    path_a.write_text(json.dumps(a))
    path_b.write_text(json.dumps(b))

    res = CliRunner().invoke(
        cli, ["bench", "compare", str(path_a), str(path_b),
             "--cost-top", "5", "--cost-threshold", "1.1"])
    assert res.exit_code == 0, res.output
    # The planted regression must be NAMED, not just counted.
    assert "s1" in res.output
    assert "+240" in res.output
    assert "spec(s) missing cost" in res.output
    assert "⚠" in res.output


def test_bench_compare_says_so_when_no_run_carries_cost_data(tmp_path):
    a = _run("base", [_row("s1", ok=True), _row("s2", ok=False)])
    b = _run("change", [_row("s1", ok=False), _row("s2", ok=True)])
    path_a = tmp_path / "run_a.json"
    path_b = tmp_path / "run_b.json"
    path_a.write_text(json.dumps(a))
    path_b.write_text(json.dumps(b))

    res = CliRunner().invoke(cli, ["bench", "compare", str(path_a), str(path_b)])
    assert res.exit_code == 0, res.output
    assert "no spec on either side" in res.output


def test_compare_sides_carry_each_runs_rederived_count():
    """`rederived_a`/`rederived_b` are read straight off each run's OWN
    aggregate (`pin_rederived_spec_count`) — never recomputed from the paired
    rows — so a baseline cut before the history rewrite (0 re-derived) can be
    compared against a change run measured entirely on re-derived pins (56)
    without either side's number drifting from what that run's own report
    would say."""
    a = _run("base", [_row("s1", ok=True), _row("s2", ok=False)])
    a["aggregate"] = {"pin_rederived_spec_count": 0}
    b = _run("change", [_row("s1", ok=True), _row("s2", ok=True)])
    b["aggregate"] = {"pin_rederived_spec_count": 56}

    cmp = compare_runs(a, b)

    assert cmp.rederived_a == 0
    assert cmp.rederived_b == 56


def test_compare_sides_default_rederived_to_zero_when_unrecorded():
    """A run whose aggregate predates `pin_rederived_spec_count` must not
    raise — it reports 0 here, same as a run that measured zero re-derived
    specs. But the 0 alone is ambiguous with a genuine zero, so
    `rederived_recorded_a/b` (set from key PRESENCE in `compare_runs`, not
    from the count) distinguishes "0" from "never recorded" right on this
    dataclass — `_compare_side` reads both fields directly."""
    a = _run("base", [_row("s1", ok=True)])
    b = _run("change", [_row("s1", ok=True)])

    cmp = compare_runs(a, b)

    assert cmp.rederived_a == 0
    assert cmp.rederived_b == 0
    assert cmp.rederived_recorded_a is False
    assert cmp.rederived_recorded_b is False


def test_compare_sides_mark_a_recorded_zero_as_recorded():
    """A run that genuinely re-derived zero pins (the field IS present, just
    0) must report `recorded is True` — only an aggregate missing the field
    entirely reports `False`."""
    a = _run("base", [_row("s1", ok=True)])
    a["aggregate"] = {"pin_rederived_spec_count": 0}
    b = _run("change", [_row("s1", ok=True)])
    b["aggregate"] = {"pin_rederived_spec_count": 0}

    cmp = compare_runs(a, b)

    assert cmp.rederived_recorded_a is True
    assert cmp.rederived_recorded_b is True


def test_compare_prints_unrecorded_when_the_run_predates_the_field(tmp_path):
    a = _run("base", [_row("s1", ok=True)])
    b = _run("change", [_row("s1", ok=True)])
    b["aggregate"] = {"pin_rederived_spec_count": 3}
    path_a = tmp_path / "run_a.json"
    path_b = tmp_path / "run_b.json"
    path_a.write_text(json.dumps(a))
    path_b.write_text(json.dumps(b))

    res = CliRunner().invoke(cli, ["bench", "compare", str(path_a), str(path_b)])
    assert res.exit_code == 0, res.output
    assert "unrecorded" in res.output
    assert not re.search(r"0\s+re-derived pin\(s\)", res.output)


def test_bench_compare_prints_each_runs_rederived_pin_count(tmp_path):
    a = _run("base", [_row("s1", ok=True), _row("s2", ok=False)])
    a["aggregate"] = {"pin_rederived_spec_count": 0}
    b = _run("change", [_row("s1", ok=False), _row("s2", ok=True)])
    b["aggregate"] = {"pin_rederived_spec_count": 56}
    path_a = tmp_path / "run_a.json"
    path_b = tmp_path / "run_b.json"
    path_a.write_text(json.dumps(a))
    path_b.write_text(json.dumps(b))

    res = CliRunner().invoke(cli, ["bench", "compare", str(path_a), str(path_b)])
    assert res.exit_code == 0, res.output
    # Rich wraps at terminal width, so the count and its label can land on
    # either side of a line break — match across whitespace, not substring.
    assert re.search(r"0\s+re-derived pin\(s\)", res.output)
    assert re.search(r"56\s+re-derived pin\(s\)", res.output)


def test_cli_cost_option_defaults_match_the_eval_module_constants():
    """`bench_compare`'s `--cost-top`/`--cost-threshold` options are literal
    defaults, not `bench_compare.DEFAULT_COST_TOP`/`DEFAULT_COST_FLIP_RATIO`
    directly — every `..eval` import in commands.py is lazy (inside function
    bodies), and a click option default is evaluated at module load, so
    referencing the eval-module constant there would force a top-level
    `..eval` import. This guard is what keeps the two from drifting apart."""
    params = {p.name: p.default for p in bench_compare.params}
    assert params["cost_top"] == DEFAULT_COST_TOP
    assert params["cost_threshold"] == DEFAULT_COST_FLIP_RATIO
