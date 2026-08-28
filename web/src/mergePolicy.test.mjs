import test from "node:test";
import assert from "node:assert/strict";
import { mergePolicyRows, mergeReadyChip } from "./slideOverSummary.js";

// mergePolicyRows() is a pure formatter over core/merge_policy.py's
// `PolicyVerdict.as_dict()` (task.context.merge_policy[sha]) — the same
// verdict `nh status`'s `merge-ready: N` line and the board's MERGE-READY
// chip (mergeReadyChip) read via api/models.py's `merge_ready_for`. Modelled
// on verifierRows.test.mjs's structure/idiom (slideOverSummary.js:819).

function verdict(overrides = {}) {
  return {
    ready: true,
    summary: "ready — 2 of 2 rules satisfied",
    source: "file",
    problems: [],
    policy_changed_in_diff: false,
    rules: [
      { name: "tests_pass", passed: true, detail: "12/12 passed" },
      { name: "no_todo", passed: true, detail: "" },
    ],
    ...overrides,
  };
}

// ── "nothing to show" cases ─────────────────────────────────────────────────

test("mergePolicyRows is null for undefined", () => {
  assert.equal(mergePolicyRows(undefined), null);
});

test("mergePolicyRows is null for null", () => {
  assert.equal(mergePolicyRows(null), null);
});

test("mergePolicyRows is null for an object with no rules array", () => {
  assert.equal(mergePolicyRows({}), null);
});

test("mergePolicyRows is null for an object with an empty rules array", () => {
  assert.equal(mergePolicyRows(verdict({ rules: [] })), null);
});

test("mergePolicyRows is null for any non-object input", () => {
  assert.equal(mergePolicyRows("not an object"), null);
  assert.equal(mergePolicyRows(42), null);
  assert.equal(mergePolicyRows([]), null);
});

// ── summary + rows mapping ──────────────────────────────────────────────────

test("mergePolicyRows carries the summary and ready flag through verbatim", () => {
  const mp = mergePolicyRows(verdict());
  assert.equal(mp.summary, "ready — 2 of 2 rules satisfied");
  assert.equal(mp.ready, true);
});

test("mergePolicyRows maps rules to {name, ok, detail} rows, preserving order", () => {
  const mp = mergePolicyRows(verdict({
    rules: [
      { name: "tests_pass", passed: false, detail: "3 failed" },
      { name: "no_todo", passed: true, detail: "clean" },
    ],
  }));
  assert.deepEqual(mp.rows, [
    { name: "tests_pass", ok: false, detail: "3 failed" },
    { name: "no_todo", ok: true, detail: "clean" },
  ]);
});

test("a rule with no detail renders an empty string, not undefined", () => {
  const mp = mergePolicyRows(verdict({ rules: [{ name: "x", passed: true, detail: undefined }] }));
  assert.equal(mp.rows[0].detail, "");
});

// ── source line ──────────────────────────────────────────────────────────────

test("source: 'file' renders the merge_policy.yaml path", () => {
  const mp = mergePolicyRows(verdict({ source: "file" }));
  assert.equal(mp.source, "file");
  assert.equal(mp.sourceLine, "policy: .no_human/merge_policy.yaml");
});

test("source: 'default' renders the built-in default line", () => {
  const mp = mergePolicyRows(verdict({ source: "default" }));
  assert.equal(mp.source, "default");
  assert.equal(mp.sourceLine, "policy: built-in default");
});

test("an unrecognised source string falls back to the default line, fail-closed", () => {
  const mp = mergePolicyRows(verdict({ source: "memory" }));
  assert.equal(mp.source, "default");
  assert.equal(mp.sourceLine, "policy: built-in default");
});

// ── problems passthrough ────────────────────────────────────────────────────

test("problems pass through verbatim", () => {
  const mp = mergePolicyRows(verdict({ problems: ["no policy file found"] }));
  assert.deepEqual(mp.problems, ["no policy file found"]);
});

test("problems is [] (not null/undefined) when the verdict carries none", () => {
  const mp = mergePolicyRows(verdict({ problems: undefined }));
  assert.deepEqual(mp.problems, []);
});

// ── policyChanged ────────────────────────────────────────────────────────────

test("policyChanged is true when policy_changed_in_diff is true", () => {
  const mp = mergePolicyRows(verdict({ policy_changed_in_diff: true }));
  assert.equal(mp.policyChanged, true);
});

test("policyChanged is false by default", () => {
  const mp = mergePolicyRows(verdict());
  assert.equal(mp.policyChanged, false);
});

// ── mergeReadyChip() ─────────────────────────────────────────────────────────

test("mergeReadyChip is 'MERGE-READY' for an awaiting_approval task with merge_ready true", () => {
  assert.equal(mergeReadyChip({ status: "awaiting_approval", merge_ready: true }), "MERGE-READY");
});

test("mergeReadyChip is 'MERGE-READY' when the server-provided lane is 'review'", () => {
  assert.equal(mergeReadyChip({ lane: "review", merge_ready: true, status: "bogus" }), "MERGE-READY");
});

test("mergeReadyChip is null when merge_ready is true but the task is not in the review lane", () => {
  assert.equal(mergeReadyChip({ status: "implementing", merge_ready: true }), null);
  assert.equal(mergeReadyChip({ status: "done", merge_ready: true }), null);
});

test("mergeReadyChip is null when merge_ready is false, even in the review lane", () => {
  assert.equal(mergeReadyChip({ status: "awaiting_approval", merge_ready: false }), null);
});

test("mergeReadyChip is null when merge_ready is null (no verdict for this head)", () => {
  assert.equal(mergeReadyChip({ status: "awaiting_approval", merge_ready: null }), null);
});

test("mergeReadyChip is null for an empty/undefined task", () => {
  assert.equal(mergeReadyChip(undefined), null);
  assert.equal(mergeReadyChip({}), null);
});
