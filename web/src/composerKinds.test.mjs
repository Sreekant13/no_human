import test from "node:test";
import assert from "node:assert/strict";
import { COMPOSER_KINDS, kindByValue, needsPrUrl } from "./composerKinds.js";

// The chip set MIRRORS the kinds the shipped modal offered (App.jsx kind <select>).
// The authoritative enum (intake/classify.py TaskKind) also has `traceability`,
// which the UI has never offered — adding it here would be a silent scope
// expansion, and dropping one of the seven would be a regression.

test("offers exactly the seven kinds the shipped UI offered", () => {
  assert.deepEqual(
    COMPOSER_KINDS.map((k) => k.kind),
    [
      "feature",
      "bugfix",
      "ci_fix",
      "test_gap",
      "investigation",
      "design_doc",
      "code_review",
    ],
  );
});

test("every chip has a human label and a hint", () => {
  for (const chip of COMPOSER_KINDS) {
    assert.ok(chip.label && chip.label.length > 0, `${chip.kind} needs a label`);
    assert.ok(chip.hint && chip.hint.length > 0, `${chip.kind} needs a hint`);
  }
});

test("labels are human-readable, not raw enum values", () => {
  assert.equal(kindByValue("design_doc").label, "Design doc");
  assert.equal(kindByValue("ci_fix").label, "CI fix");
});

test("kindByValue returns undefined for an unknown kind (never throws)", () => {
  assert.equal(kindByValue("traceability"), undefined);
  assert.equal(kindByValue(""), undefined);
  assert.equal(kindByValue(null), undefined);
});

test("code_review is the only kind that requires a PR reference", () => {
  // orchestrator.py:2872 hard-fails a code_review task with no PR/MR ref.
  assert.equal(needsPrUrl("code_review"), true);
  for (const chip of COMPOSER_KINDS) {
    if (chip.kind !== "code_review") {
      assert.equal(needsPrUrl(chip.kind), false, `${chip.kind} must not require a URL`);
    }
  }
  assert.equal(needsPrUrl(null), false);
});
