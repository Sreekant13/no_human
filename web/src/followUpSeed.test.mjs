import test from "node:test";
import assert from "node:assert/strict";
import { followUpSeed } from "./followUpSeed.js";

test("first line names the predecessor's short id and title", () => {
  const seed = followUpSeed({
    id: "abcdef1234567890",
    title: "Fix the flaky retry",
    kind: "bugfix",
    repo_path: "/tmp/repo",
  });
  assert.equal(seed.prompt.split("\n")[0], "Follow-up to abcdef12: Fix the flaky retry");
});

test("carries followsId, kind, and repoPath through untouched", () => {
  const seed = followUpSeed({
    id: "abcdef1234567890",
    title: "Fix the flaky retry",
    kind: "bugfix",
    repo_path: "/tmp/repo",
  });
  assert.equal(seed.followsId, "abcdef1234567890");
  assert.equal(seed.kind, "bugfix");
  assert.equal(seed.repoPath, "/tmp/repo");
});

test("defaults kind to feature when the predecessor has none", () => {
  const seed = followUpSeed({ id: "abcdef1234567890", title: "x", repo_path: null });
  assert.equal(seed.kind, "feature");
});

test("includes the previous PR link when the predecessor shipped one", () => {
  const withPr = followUpSeed({
    id: "abcdef1234567890", title: "x", pr_url: "https://github.com/x/y/pull/1",
  });
  assert.ok(withPr.prompt.includes("Previous PR: https://github.com/x/y/pull/1"));

  const withoutPr = followUpSeed({ id: "abcdef1234567890", title: "x" });
  assert.ok(!withoutPr.prompt.includes("Previous PR:"));
});
