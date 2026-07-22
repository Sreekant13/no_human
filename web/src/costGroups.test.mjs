import test from "node:test";
import assert from "node:assert/strict";
import { costByProject, totalCost, UNATTRIBUTED } from "./costGroups.js";
import { taskCost } from "./cost.js";
import { ledgerSummary } from "./nightLedger.js";

// Pure aggregation over the cost model — the kind of logic this repo tests directly
// (boardLanes, searchView, learningGroups all follow this shape).

const task = (repo, used, read = 0, reviewUsed = 0, auxUsed = 0) => ({
  repo_name: repo,
  total_tokens: used,
  total_cache_read: read,
  total_review_tokens: reviewUsed,
  total_aux_tokens: auxUsed,
});

test("groups by repo and sums each project's cost", () => {
  const rows = costByProject([task("a", 1000), task("b", 2000), task("a", 3000)]);
  assert.deepEqual(rows.map((r) => [r.project, r.tasks]), [["a", 2], ["b", 1]]);
  assert.equal(rows.find((r) => r.project === "a").cost, taskCost(task("a", 4000)));
});

test("sorts by cost desc, then by name for a stable order", () => {
  const rows = costByProject([task("cheap", 1), task("dear", 9999), task("mid", 500)]);
  assert.deepEqual(rows.map((r) => r.project), ["dear", "mid", "cheap"]);
  // Equal cost -> alphabetical, so the list does not reshuffle between renders.
  const tied = costByProject([task("zeta", 100), task("alpha", 100)]);
  assert.deepEqual(tied.map((r) => r.project), ["alpha", "zeta"]);
});

test("counts the reviewer's tokens, not just the coder's", () => {
  const [row] = costByProject([task("a", 1000, 0, 1000)]);
  const coderOnly = taskCost({ total_tokens: 1000 });
  assert.ok(row.cost > coderOnly, "review tokens were dropped from the rollup");
});

test("counts the aux (planning/utility) bucket too", () => {
  // The first version of taskCost() omitted aux, so this rollup priced a smaller run than
  // the sidebar ledger listing the same tasks — two surfaces, two numbers.
  const [row] = costByProject([task("a", 1000, 0, 0, 1000)]);
  const withoutAux = taskCost({ total_tokens: 1000 });
  assert.ok(row.cost > withoutAux, "aux tokens were dropped from the rollup");
  // ...and it must price aux exactly like the other fresh buckets, not at some other rate.
  assert.equal(row.cost, taskCost({ total_tokens: 2000 }));
});

test("agrees with the sidebar ledger for the same tasks", () => {
  // The real cross-surface check. An earlier version of this test asserted
  // `row.cost === taskCost(t)` — but costByProject CALLS taskCost, so it asserted x === x
  // and would have stayed green through the very bug that failed this branch's review.
  // This one actually runs the ledger.
  const now = Date.now();
  const recent = (over) => ({
    ...over,
    updated_at: new Date(now - 3600e3).toISOString(),
    created_at: new Date(now - 7200e3).toISOString(),
    status: "done",
  });
  const tasks = [
    recent(task("a", 1_000_000, 0, 0, 2_000_000)),   // aux-heavy: the bucket that drifted
    recent(task("b", 1_000_000)),
  ];
  const rollup = totalCost(costByProject(tasks));
  assert.ok(rollup > 0, "fixture priced at zero — the assertion below would be vacuous");
  assert.ok(
    Math.abs(ledgerSummary(tasks, now).cost - rollup) < 1e-9,
    "the Stats rollup and the sidebar ledger price the same tasks differently",
  );
});

test("groups case-insensitively, keeping the first spelling seen", () => {
  const rows = costByProject([task("no_human", 1000), task("No_Human", 1000)]);
  assert.equal(rows.length, 1, "the same repo in two casings split into two rows");
  assert.equal(rows[0].tasks, 2);
  assert.equal(rows[0].project, "no_human", "should display the spelling seen first");
});

test("keeps tasks with no repo instead of dropping them", () => {
  const rows = costByProject([task("a", 1000), task(undefined, 2000), task("   ", 3000)]);
  const un = rows.find((r) => r.project === UNATTRIBUTED);
  assert.equal(un.tasks, 2, "tasks with a missing or blank repo were lost");
});

test("the rows always sum to the total", () => {
  const tasks = [task("a", 1000, 500), task("b", 2000), task(null, 300, 0, 700)];
  const rows = costByProject(tasks);
  const expected = tasks.reduce((s, t) => s + taskCost(t), 0);
  // Floating point: compare within a cent rather than exactly.
  assert.ok(Math.abs(totalCost(rows) - expected) < 0.01);
});

test("empty and missing input do not throw", () => {
  assert.deepEqual(costByProject([]), []);
  assert.deepEqual(costByProject(undefined), []);
  assert.equal(totalCost([]), 0);
  assert.equal(totalCost(undefined), 0);
});
