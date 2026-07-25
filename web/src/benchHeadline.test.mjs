import test from "node:test";
import assert from "node:assert/strict";
import { selectBenchHeadline, footnoteReasons, footnoteLabel } from "./benchHeadline.js";

const baseline = {
  label: "v14", created_at: "2026-07-20T00:00:00+00:00",
  success_rate: 0.46, satisfied: 26, total: 57,
  honest_escalation_rate: 0.77, honest_escalations: 10, escalation_specs: 13,
  median_cost_ratio: 0.107, corpus_available: 57,
  refusals: [], override_reasons: [],
};

const probe = {
  label: "health-probe", created_at: "2026-07-24T09:00:00+00:00",
  success_rate: 0, satisfied: 0, total: 1,
  refusals: ["only 1 spec(s) ran (minimum 10) — a probe or a capped run is a slice, not the corpus"],
  override_reasons: [],
};

test("a published baseline headlines with its own fields intact", () => {
  const { headline, footnote } = selectBenchHeadline([baseline], null);
  assert.equal(headline, baseline);
  assert.equal(headline.label, "v14");
  assert.equal(headline.success_rate, 0.46);
  assert.equal(headline.honest_escalation_rate, 0.77);
  assert.equal(headline.median_cost_ratio, 0.107);
  assert.equal(headline.corpus_available, 57);
  assert.equal(headline.created_at, "2026-07-20T00:00:00+00:00");
  assert.equal(footnote, null);
});

test("a newer refused/probe run renders as a footnote, never the headline", () => {
  const { headline, footnote } = selectBenchHeadline([baseline], probe);
  assert.equal(headline, baseline, "the probe must never become the headline");
  assert.equal(footnote, probe);
  assert.equal(
    footnoteLabel(footnote),
    "latest run - refused publication: only 1 spec(s) ran (minimum 10) — a probe or a capped run is a slice, not the corpus");
});

test("multiple published baselines: the headline is the most recent by date", () => {
  const older = { ...baseline, label: "v13", created_at: "2026-07-10T00:00:00+00:00" };
  const newer = { ...baseline, label: "v14", created_at: "2026-07-20T00:00:00+00:00" };
  const { headline } = selectBenchHeadline([older, newer], null);
  assert.equal(headline, newer);
  const { headline: headline2 } = selectBenchHeadline([newer, older], null);
  assert.equal(headline2, newer, "order in the array must not matter");
});

test("no published baseline (empty array): current behavior preserved — no headline, no footnote", () => {
  assert.deepEqual(selectBenchHeadline([], probe), { headline: null, footnote: null });
});

test("no published baseline (null): current behavior preserved — no headline, no footnote", () => {
  assert.deepEqual(selectBenchHeadline(null, probe), { headline: null, footnote: null });
});

test("a latest run that is NOT newer than the headline produces no footnote", () => {
  const sameOrOlder = { ...probe, created_at: "2026-07-19T00:00:00+00:00" };
  const { footnote } = selectBenchHeadline([baseline], sameOrOlder);
  assert.equal(footnote, null);
});

test("a latest run identical to the headline (it WAS published) produces no footnote", () => {
  const { footnote } = selectBenchHeadline([baseline], baseline);
  assert.equal(footnote, null);
});

test("footnoteReasons falls back to override_reasons when a forced publish left no refusals recorded", () => {
  const forced = { label: "forced", created_at: "2026-07-24T09:00:00+00:00",
                   refusals: [], override_reasons: ["only 3 spec(s) ran (minimum 10)"] };
  assert.deepEqual(footnoteReasons(forced), ["only 3 spec(s) ran (minimum 10)"]);
  assert.equal(footnoteLabel(forced),
    "latest run - refused publication: only 3 spec(s) ran (minimum 10)");
});

test("footnoteReasons is empty, and footnoteLabel says so, when nothing is missing but no reasons exist", () => {
  const mystery = { label: "?", created_at: "2026-07-24T09:00:00+00:00", refusals: [], override_reasons: [] };
  assert.deepEqual(footnoteReasons(mystery), []);
  assert.equal(footnoteLabel(mystery), "latest run - refused publication: not publishable");
});

test("footnoteReasons/footnoteLabel on a null footnote never throw", () => {
  assert.deepEqual(footnoteReasons(null), []);
  assert.equal(footnoteLabel(null), "latest run - refused publication: not publishable");
});
