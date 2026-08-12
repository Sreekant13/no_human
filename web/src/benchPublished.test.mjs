import test from "node:test";
import assert from "node:assert/strict";
import { selectPublishedRun, publishedEscalationPct, formatSuccessFraction,
         benchSpecCounts, benchSuccessText, benchSuccessDenominator } from "./benchPublished.js";

const publishedBaseline = {
  published: true,
  label: "v14", created_at: "2026-07-20T00:00:00+00:00",
  success_rate: 0.46, satisfied: 26, total: 57, skipped: 0,
  honest_escalations: 13, escalation_specs: 15, honest_escalation_rate: 0.77,
  median_cost_ratio: 0.107, corpus_available: 57,
};

test("published baseline yields the trusted figures", () => {
  const row = selectPublishedRun(publishedBaseline);
  assert.deepEqual(row, {
    label: "v14",
    created_at: "2026-07-20T00:00:00+00:00",
    success_rate: 0.46,
    satisfied: 26,
    total: 57,
    skipped: 0,
    honest_escalations: 13,
    escalation_specs: 15,
    honest_escalation_rate: 0.77,
    median_cost_ratio: 0.107,
    // northstar bench cost ratio part 2: `undefined` on this fixture, which
    // predates the basis label — carried through when the backend sends it
    // (see the dedicated test below).
    median_cost_ratio_basis: undefined,
    // bench-v2 V1 fields. `undefined` here because this fixture is a
    // pre-trials card — the point of the deepEqual is that the projection is
    // an exhaustive whitelist, so a new field the backend publishes and this
    // list forgets shows up as a FAILURE here rather than as a silently bare
    // percentage in the panel.
    spec_mean_success_rate: undefined,
    success_ci_low: undefined,
    success_ci_high: undefined,
    trials: undefined,
    specs: undefined,
    ran_specs: undefined,
    // P0.1: the measured population's own fraction (deaths excluded) —
    // undefined on this pre-P0.1 fixture, carried when the backend sends it.
    measured_specs: undefined,
    measured_satisfied: undefined,
    dead_specs: undefined,
    dead_spec_count: undefined,
  });
  assert.equal(publishedEscalationPct(row), "87% (13/15)");
});

// northstar bench cost ratio part 2: the basis label must travel WITH the
// ratio through the whitelist, or a reader of "Last published run" cannot
// tell a tier-weighted figure apart from one computed on the older,
// role-blind cache-only basis.
test("the basis label travels with the cost ratio through the whitelist", () => {
  const tierWeighted = { ...publishedBaseline, median_cost_ratio_basis: "tier-weighted" };
  const row = selectPublishedRun(tierWeighted);
  assert.equal(row.median_cost_ratio, 0.107);
  assert.equal(row.median_cost_ratio_basis, "tier-weighted");
});

test("no published run → null", () => {
  assert.equal(selectPublishedRun(undefined), null);
  assert.equal(selectPublishedRun(null), null);
  assert.equal(selectPublishedRun({ norun: true }), null);
  const noPublishedKey = { ...publishedBaseline };
  delete noPublishedKey.published;
  assert.equal(selectPublishedRun(noPublishedKey), null);
});

test("a refused/probe run is never sourced", () => {
  const refused = {
    label: "health-probe", created_at: "2026-07-24T09:00:00+00:00",
    success_rate: 0, satisfied: 0, total: 1,
    refusals: ["only 1 spec(s) ran (minimum 10) — a probe or a capped run is a slice, not the corpus"],
  };
  assert.equal(selectPublishedRun(refused), null);

  const explicitlyFalse = { ...publishedBaseline, published: false };
  assert.equal(selectPublishedRun(explicitlyFalse), null);
});

// Success is over RAN specs (total - skipped), never the loaded total —
// pinning the actual v13 published shape (56 loaded, 3 skipped, 53 ran, 25
// satisfied) so the row can never regress to "(25/56)" (README.md:116 and
// docs/NORTH_STAR_BENCH.md:8 both publish 25/53 = 47%).
test("success fraction is over RAN specs, not loaded total (v13 pin)", () => {
  const v13 = { ...publishedBaseline, total: 56, skipped: 3, satisfied: 25, success_rate: 25 / 53 };
  const row = selectPublishedRun(v13);
  assert.equal(row.skipped, 3);
  const pct = Math.round(row.success_rate * 100);
  assert.equal(`${pct}%${formatSuccessFraction(row.satisfied, row.total, row.skipped)}`, "47% (25/53)");
});

// P0.1: on a dead-carrying card the headline rate is measured over the specs
// that did priced work, so the fraction beside it must reduce to that
// percentage — the pooled pair (satisfied over every ran row) no longer does.
test("the success fraction reduces to the headline on a dead-carrying card", () => {
  // 5 rows ran, 1 died before any model call: 4 measured, 2 passed → 50%.
  const deadRun = {
    ...publishedBaseline,
    total: 5, skipped: 0, satisfied: 2, dead_specs: 1, dead_spec_count: 1,
    trials: 1, specs: 5, ran_specs: 5,
    measured_specs: 4, measured_satisfied: 2,
    success_rate: 0.5, spec_mean_success_rate: 0.5,
    success_ci_low: 0.15, success_ci_high: 0.85,
  };
  const row = selectPublishedRun(deadRun);
  // The whitelist carries the measured pair — dropping either key silently
  // reverts the fraction to the pooled pair on the "last published" row.
  assert.equal(row.measured_specs, 4);
  assert.equal(row.measured_satisfied, 2);
  const fraction = benchSuccessDenominator(row);
  assert.equal(fraction, " (2/4)");
  // The reduction the test is named for: 2/4 IS the 50% in front of it…
  assert.equal(row.measured_satisfied / row.measured_specs,
               row.spec_mean_success_rate);
  // …whereas the pooled pair (2/5 = 40%) is NOT, which is why it must not be
  // what renders beside the headline.
  assert.notEqual(fraction, " (2/5)");
  assert.notEqual(row.satisfied / (row.total - row.skipped),
                  row.spec_mean_success_rate);
});

// An older card (no measured_* keys) keeps the pre-P0.1 pair — where the
// headline itself was computed over ran, so the pair still matches it.
test("a card without measured keys falls back to the ran pair", () => {
  const legacy = { ...publishedBaseline, total: 56, skipped: 3, satisfied: 25,
    measured_specs: undefined, measured_satisfied: undefined };
  const row = selectPublishedRun(legacy);
  assert.equal(benchSuccessDenominator(row), " (25/53)");
});

test("success fraction is omitted (not wrong) when skipped is unknown", () => {
  assert.equal(formatSuccessFraction(25, 56, undefined), "");
  assert.equal(formatSuccessFraction(25, 56, NaN), "");
  assert.equal(formatSuccessFraction(undefined, 56, 3), "");
});

test("escalation with zero denominator does not fake 100%", () => {
  // The backend nulls honest_escalation_rate whenever escalation_specs is 0
  // (app.py _bench_payload: "honest_escalation_rate": agg["honest_escalation_rate"]
  // if agg["escalation_specs"] else None) — this is the only zero-denominator
  // shape the backend can actually emit; a truthy rate alongside specs:0 is
  // not a real payload.
  const zeroDenom = { ...publishedBaseline, escalation_specs: 0, honest_escalation_rate: null };
  const row = selectPublishedRun(zeroDenom);
  assert.equal(publishedEscalationPct(row), "— (denominator unknown)");

  const nothingKnown = { ...publishedBaseline, escalation_specs: 0, honest_escalation_rate: undefined,
    honest_escalations: undefined };
  const row2 = selectPublishedRun(nothingKnown);
  assert.equal(publishedEscalationPct(row2), "— (denominator unknown)");

  assert.equal(publishedEscalationPct(null), "—");
});

// ---------------------------------------------------------------------------
// bench-v2 V1 review (SHOULD 4): the panel read ROW counts as SPEC counts, and
// printed a percentage with no interval beside a card that records one.
// ---------------------------------------------------------------------------

// 20 specs of a 55-spec corpus, replayed 3x. `total` is 60 ROWS.
const trialsRun = {
  published: true,
  label: "v16", created_at: "2026-08-03T00:00:00+00:00",
  total: 60, skipped: 0, satisfied: 44, dead_specs: 3, dead_spec_count: 1,
  trials: 3, specs: 20, ran_specs: 20, corpus_available: 55,
  success_rate: 0.7333, spec_mean_success_rate: 0.7333,
  success_ci_low: 0.5089, success_ci_high: 0.8781,
};

test("corpus coverage is counted in SPECS: 20 of 55, never 60 of 55", () => {
  const { specs } = benchSpecCounts(trialsRun);
  assert.equal(specs, 20);
  assert.equal(`${specs} of ${trialsRun.corpus_available} loaded`, "20 of 55 loaded");
  // The bug: `total` is rows, so the panel rendered a filtered 20-spec slice
  // as MORE than the whole 55-spec corpus — and `total < corpus` (the
  // under-coverage alarm) was false, so it alarmed on nothing.
  assert.ok(trialsRun.total > trialsRun.corpus_available, "precondition: rows exceed the corpus");
  assert.ok(specs < trialsRun.corpus_available, "specs must trip the under-coverage alarm");
});

test("a dead spec is one spec, not one per trial", () => {
  const { deadSpecs, unmeasured } = benchSpecCounts(trialsRun);
  assert.equal(deadSpecs, 1, "3 dead ROWS are one spec that died in all three trials");
  assert.equal(unmeasured, 1);
});

test("a pre-trials card falls back to the row fields, which ARE its spec counts", () => {
  // Not an approximation: at one trial per spec, rows and specs are the same
  // number, so the fallback is exact and today's rendering does not move.
  const { specs, ranSpecs, deadSpecs, unmeasured, trials } = benchSpecCounts(publishedBaseline);
  assert.equal(specs, 57);
  assert.equal(ranSpecs, 57);
  assert.equal(deadSpecs, 0);
  assert.equal(unmeasured, 0);
  assert.equal(trials, 1);
});

test("benchSpecCounts on an absent/empty card never throws and never invents", () => {
  assert.deepEqual(benchSpecCounts(null),
    { specs: 0, ranSpecs: 0, deadSpecs: 0, trials: 1, skippedSpecs: 0, unmeasured: 0 });
  assert.deepEqual(benchSpecCounts({}),
    { specs: 0, ranSpecs: 0, deadSpecs: 0, trials: 1, skippedSpecs: 0, unmeasured: 0 });
});

test("skipped specs count as unmeasured, in specs", () => {
  const partly = { total: 30, skipped: 12, specs: 10, ran_specs: 6, trials: 3,
                   dead_spec_count: 1 };
  const { specs, ranSpecs, skippedSpecs, unmeasured } = benchSpecCounts(partly);
  assert.equal(specs, 10);
  assert.equal(ranSpecs, 6);
  assert.equal(skippedSpecs, 4);
  assert.equal(unmeasured, 5, "4 never ran + 1 ran and burned nothing");
});

test("the success figure carries its interval when the card records one", () => {
  assert.equal(benchSuccessText(trialsRun), "73% (95% CI 50.9–87.8)");
});

test("point estimate and interval come from the SAME estimator", () => {
  // The defect, as the reviewer drove it: 11 specs x 20 passing trials plus one
  // spec that got a single failing trial. The pooled ROW rate is 99.5%; the
  // published figure is the mean of per-spec means, 91.7%, and the interval
  // belongs to THAT. A panel reading `success_rate` would print 100% beside an
  // interval computed for 91.7%.
  const resumed = {
    total: 221, skipped: 0, satisfied: 220, specs: 12, ran_specs: 12, trials: 20,
    success_rate: 0.9955, spec_mean_success_rate: 0.9167,
    success_ci_low: 0.6461, success_ci_high: 0.9851,
  };
  assert.equal(benchSuccessText(resumed), "92% (95% CI 64.6–98.5)");
  assert.notEqual(Math.round(resumed.success_rate * 100), Math.round(resumed.spec_mean_success_rate * 100));
});

test("a card with no interval renders a bare percentage rather than an invented range", () => {
  assert.equal(benchSuccessText(publishedBaseline), "46%");
  assert.equal(benchSuccessText({ success_ci_low: 0.5 }), "—", "half an interval is no interval");
  assert.equal(benchSuccessText(null), "—");
});

test("the denominator is stated in SPECS, and states the sample shape under trials", () => {
  // One trial per spec: the familiar pair, with `ran_specs` as its denominator
  // instead of `total - skipped` in rows. Identical here, right under --trials.
  assert.equal(benchSuccessDenominator(publishedBaseline), " (26/57)");
  // Above one trial `satisfied` counts passing TRIALS (220 of 221) while the
  // percentage in front is a mean of per-spec means — no fraction between them
  // reduces, so none is printed.
  assert.equal(benchSuccessDenominator(trialsRun), " (over 20 specs × 3 trials)");
  assert.equal(benchSuccessDenominator({}), "");
});

test("the published row carries the interval fields through the whitelist", () => {
  // selectPublishedRun projects a fixed key list; a field it forgets is absent
  // downstream and benchSuccessText silently falls back to a bare pooled
  // percentage — on the one row labelled "last published run".
  const row = selectPublishedRun(trialsRun);
  assert.equal(row.spec_mean_success_rate, 0.7333);
  assert.equal(row.success_ci_low, 0.5089);
  assert.equal(row.success_ci_high, 0.8781);
  assert.equal(row.specs, 20);
  assert.equal(row.ran_specs, 20);
  assert.equal(row.trials, 3);
  assert.equal(row.dead_spec_count, 1);
  assert.equal(benchSuccessText(row), "73% (95% CI 50.9–87.8)");
  assert.equal(benchSuccessDenominator(row), " (over 20 specs × 3 trials)");
});
