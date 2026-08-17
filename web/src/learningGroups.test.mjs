import test from "node:test";
import assert from "node:assert/strict";
import { groupLearningsByProject, projectLabel } from "./learningGroups.js";

test("projectLabel is the last path segment, or Unscoped when empty", () => {
  assert.equal(projectLabel("/Users/e/git/acme/master/no_human"), "no_human");
  assert.equal(projectLabel("/Users/e/git/metrics-core/"), "metrics-core");   // trailing slash
  assert.equal(projectLabel(""), "Unscoped");
  assert.equal(projectLabel(null), "Unscoped");
});

test("proposals group by project with counts", () => {
  const items = [
    { id: "1", project: "/Users/e/git/metrics-core" },
    { id: "2", project: "/Users/e/git/metrics-core" },
    { id: "3", project: "/Users/e/git/no_human" },
    { id: "4", project: "" },
    { id: "5", project: null },
  ];
  const groups = groupLearningsByProject(items);
  assert.equal(groups.length, 3);
  const metricsCore = groups.find((g) => g.label === "metrics-core");
  assert.equal(metricsCore.count, 2);
  const unscoped = groups.find((g) => g.label === "Unscoped");
  assert.equal(unscoped.count, 2);  // "" and null merge
});

test("groups sort by size, unscoped last on a tie, alpha within ties", () => {
  const items = [
    { id: "1", project: "/git/zeta" },
    { id: "2", project: "/git/alpha" },
    { id: "3", project: "" },          // one unscoped
    { id: "4", project: "/git/big" },
    { id: "5", project: "/git/big" },  // big has 2 → leads
  ];
  const groups = groupLearningsByProject(items);
  assert.deepEqual(groups.map((g) => g.label), ["big", "alpha", "zeta", "Unscoped"]);
});

test("the unscoped conversation-mining backlog surfaces as one big group", () => {
  // The live shape: a wall of mined rules, all project-less, plus a couple
  // of properly-scoped ones — the human sees the backlog as a single group.
  const items = [
    ...Array.from({ length: 131 }, (_, i) => ({ id: `m${i}`, project: "" })),
    { id: "s1", project: "/git/no_human" },
  ];
  const groups = groupLearningsByProject(items);
  assert.equal(groups[0].label, "Unscoped");
  assert.equal(groups[0].count, 131);
  assert.equal(groups[1].label, "no_human");
});

test("empty input yields no groups", () => {
  assert.deepEqual(groupLearningsByProject([]), []);
  assert.deepEqual(groupLearningsByProject(null), []);
});
