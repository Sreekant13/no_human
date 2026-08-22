import test from "node:test";
import assert from "node:assert/strict";
import { verifierRows } from "./slideOverSummary.js";

// verifierRows() is a pure formatter over `attempt.verifier_results`
// (review/verifiers.py's VerifierResult.as_dict(), one dict per rule) — the
// same JSON column the Review tab's per-verifier list renders from. It must
// agree with core/pr_evidence.py's `verifiers_pin()` on the summary-string
// shape ("N of N satisfied" / "K of N failed — id1, id2") since an operator
// reading the PR body and the board's Review tab side by side must see the
// same verdict, not two independently-worded ones.

function verifier(overrides = {}) {
  return {
    verifier_id: "no-todo",
    passed: true,
    no_verdict: false,
    evidence: "no TODOs found",
    file: "",
    line: 0,
    comment: "",
    severity: "high",
    files_checked: ["a.py", "b.py"],
    tokens_used: 100,
    ...overrides,
  };
}

// ── "nothing to show" cases ─────────────────────────────────────────────────

test("verifierRows is null for undefined (column never populated for this attempt)", () => {
  assert.equal(verifierRows(undefined), null);
});

test("verifierRows is null for an empty array (verifiers ran, matched nothing)", () => {
  assert.equal(verifierRows([]), null);
});

test("verifierRows is null for any non-array input, not just the empty-array case", () => {
  assert.equal(verifierRows(null), null);
  assert.equal(verifierRows("not an array"), null);
  assert.equal(verifierRows(42), null);
  assert.equal(verifierRows({}), null);
});

// ── summary string ──────────────────────────────────────────────────────────

test("all-pass summary reads 'N of N satisfied'", () => {
  const { summary } = verifierRows([verifier({ verifier_id: "a" }), verifier({ verifier_id: "b" })]);
  assert.equal(summary, "2 of 2 satisfied");
});

test("a mixed result reads 'K of N failed — ids', sorted", () => {
  const results = [
    verifier({ verifier_id: "no-todo", passed: true }),
    verifier({ verifier_id: "no-print", passed: false, file: "c.py", line: 4, evidence: "found a print()" }),
    verifier({ verifier_id: "docs-updated", passed: false, evidence: "docs not touched" }),
  ];
  const { summary } = verifierRows(results);
  // sorted alphabetically, not by input order (docs-updated < no-print)
  assert.equal(summary, "2 of 3 failed — docs-updated, no-print");
});

test("summary shape matches core/pr_evidence.py's verifiers_pin() wording exactly", () => {
  const allPass = verifierRows([verifier({ verifier_id: "a" }), verifier({ verifier_id: "b" })]);
  assert.match(allPass.summary, /^\d+ of \d+ satisfied$/);
  const oneFail = verifierRows([verifier({ verifier_id: "a" }), verifier({ verifier_id: "b", passed: false })]);
  assert.match(oneFail.summary, /^\d+ of \d+ failed — .+$/);
});

// ── per-row shape ───────────────────────────────────────────────────────────

test("a passing row carries id, ok, and a filesChecked count — no location or comment", () => {
  const { rows } = verifierRows([verifier({ verifier_id: "no-todo", files_checked: ["a.py", "b.py", "c.py"] })]);
  assert.deepEqual(rows, [
    { id: "no-todo", ok: true, filesChecked: 3, location: "", comment: "" },
  ]);
});

test("a failing row carries file:line as location and evidence as comment", () => {
  const { rows } = verifierRows([
    verifier({ verifier_id: "no-print", passed: false, file: "c.py", line: 4, evidence: "found a print()" }),
  ]);
  assert.deepEqual(rows, [
    // files_checked is still whatever the verifier recorded, even on a
    // failure — filesChecked is not a pass-only field, only location/comment are.
    { id: "no-print", ok: false, filesChecked: 2, location: "c.py:4", comment: "found a print()" },
  ]);
});

test("a failing row with a file but no line renders the bare file as location", () => {
  const { rows } = verifierRows([
    verifier({ verifier_id: "docs-updated", passed: false, file: "docs/x.md", line: 0, evidence: "stale" }),
  ]);
  assert.equal(rows[0].location, "docs/x.md");
});

test("a failing row with no file at all renders an empty location, not 'undefined' or ':0'", () => {
  const { rows } = verifierRows([
    verifier({ verifier_id: "docs-updated", passed: false, file: "", line: 0, evidence: "docs not touched" }),
  ]);
  assert.equal(rows[0].location, "");
  assert.equal(rows[0].comment, "docs not touched");
});

test("a failing row falls back to `comment` when `evidence` is absent (nullish, not merely empty)", () => {
  const record = verifier({ verifier_id: "x", passed: false, comment: "fallback text" });
  delete record.evidence;
  const { rows } = verifierRows([record]);
  assert.equal(rows[0].comment, "fallback text");
});

test("no_verdict (fail-closed) rows still render as a failing row — no special-case dropping them", () => {
  const { rows, summary } = verifierRows([
    verifier({ verifier_id: "no-todo", passed: false, no_verdict: true, evidence: "", severity: "high" }),
  ]);
  assert.equal(rows[0].ok, false);
  assert.equal(rows[0].id, "no-todo");
  assert.match(summary, /1 of 1 failed — no-todo/);
});

test("row order mirrors input order (only the failed-ids list in the summary is sorted)", () => {
  const results = [
    verifier({ verifier_id: "zeta", passed: false, evidence: "e" }),
    verifier({ verifier_id: "alpha", passed: true }),
  ];
  const { rows } = verifierRows(results);
  assert.deepEqual(rows.map((r) => r.id), ["zeta", "alpha"]);
});

test("a missing/malformed record (no verifier_id, no passed) still produces a row rather than throwing", () => {
  assert.doesNotThrow(() => verifierRows([{}]));
  const { rows, summary } = verifierRows([{}]);
  assert.equal(rows[0].id, "");
  assert.equal(rows[0].ok, false);
  assert.match(summary, /1 of 1 failed/);
});
