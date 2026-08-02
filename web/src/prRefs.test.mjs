import test from "node:test";
import assert from "node:assert/strict";
import { parsePrRefs, hasPrRef } from "./prRefs.js";

// This module MIRRORS src/no_human/vcs/pr_refs.py. A code_review task hard-fails
// at the gate (orchestrator.py:2872) unless parse_pr_refs finds a ref in the
// title/description, so the composer must accept exactly what the backend does —
// a wrong-negative here blocks a legitimate review. The cases below are taken
// from that module's own docstring.

test("finds a full GitHub-style pull URL", () => {
  assert.deepEqual(
    parsePrRefs("please look at https://github.com/foo/bar/pull/7001 today"),
    ["https://github.com/foo/bar/pull/7001"],
  );
});

test("finds a full GitLab merge_requests URL", () => {
  assert.deepEqual(
    parsePrRefs("https://gitlab.acme.net/ci_gate/metrics-core/-/merge_requests/7006"),
    ["https://gitlab.acme.net/ci_gate/metrics-core/-/merge_requests/7006"],
  );
});

test("expands GitHub shorthand 'host/path PR #n' to a canonical URL", () => {
  assert.deepEqual(
    parsePrRefs("code.example.com/dev/acme-test PR #7001"),
    ["https://code.example.com/dev/acme-test/pull/7001"],
  );
});

test("expands GitLab shorthand 'host path MR !n' to a canonical URL", () => {
  assert.deepEqual(
    parsePrRefs("gitlab.acme.net ci_gate/subgroup/metrics-core MR !7006"),
    ["https://gitlab.acme.net/ci_gate/subgroup/metrics-core/-/merge_requests/7006"],
  );
});

test("de-duplicates, preserving first-seen order", () => {
  const text =
    "https://x.com/a/b/pull/2 and https://x.com/a/b/pull/1 and https://x.com/a/b/pull/2";
  assert.deepEqual(parsePrRefs(text), [
    "https://x.com/a/b/pull/2",
    "https://x.com/a/b/pull/1",
  ]);
});

test("returns [] for text with no reference, and never throws on empty input", () => {
  assert.deepEqual(parsePrRefs("fix the flaky login test"), []);
  assert.deepEqual(parsePrRefs(""), []);
  assert.deepEqual(parsePrRefs(null), []);
  assert.deepEqual(parsePrRefs(undefined), []);
});

test("hasPrRef is the boolean guard the composer gates submit on", () => {
  assert.equal(hasPrRef("https://github.com/foo/bar/pull/9"), true);
  assert.equal(hasPrRef("code.example.com/dev/acme-test PR #1"), true);
  assert.equal(hasPrRef("review my branch please"), false);
  assert.equal(hasPrRef(null), false);
});
