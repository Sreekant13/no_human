import test from "node:test";
import assert from "node:assert/strict";
import { selectPublishedRun, publishedEscalationPct, formatSuccessFraction } from "./benchPublished.js";

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
  });
  assert.equal(publishedEscalationPct(row), "87% (13/15)");
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
