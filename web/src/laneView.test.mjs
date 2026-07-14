import { test } from "node:test";
import assert from "node:assert/strict";
import { topByRecency, topPrioritised } from "./laneView.js";

const t = (id, ts) => ({ id, updated_at: ts });

test("returns the N most-recently-updated, newest first", () => {
  const items = [t("a", "2026-07-01"), t("b", "2026-07-03"), t("c", "2026-07-02")];
  const { visible, hiddenCount } = topByRecency(items, 2);
  assert.deepEqual(visible.map((x) => x.id), ["b", "c"]);
  assert.equal(hiddenCount, 1);
});

test("stable order for ties (equal ts keeps input order)", () => {
  const items = [t("a", "2026-07-02"), t("b", "2026-07-02"), t("c", "2026-07-02")];
  const { visible } = topByRecency(items, 3);
  assert.deepEqual(visible.map((x) => x.id), ["a", "b", "c"]);
});

test("N >= length returns all with hiddenCount 0", () => {
  const items = [t("a", "2026-07-01"), t("b", "2026-07-02")];
  const { visible, hiddenCount } = topByRecency(items, 5);
  assert.equal(visible.length, 2);
  assert.equal(hiddenCount, 0);
});

test("empty / undefined input is safe", () => {
  assert.deepEqual(topByRecency([], 4), { visible: [], hiddenCount: 0 });
  assert.deepEqual(topByRecency(undefined, 4), { visible: [], hiddenCount: 0 });
});

test("n <= 0 hides everything, never throws", () => {
  const items = [t("a", "2026-07-01"), t("b", "2026-07-02")];
  assert.deepEqual(topByRecency(items, 0), { visible: [], hiddenCount: 2 });
});

test("null / missing timestamps sort last and never throw", () => {
  const items = [t("a", null), t("b", "2026-07-02"), { id: "c" }];
  const { visible, hiddenCount } = topByRecency(items, 1);
  assert.deepEqual(visible.map((x) => x.id), ["b"]);
  assert.equal(hiddenCount, 2);
});

test("recency falls back last_activity || updated_at || created_at", () => {
  const items = [
    { id: "old", updated_at: "2026-07-01" },
    { id: "new", created_at: "2026-06-01", last_activity: "2026-07-09" },
  ];
  const { visible } = topByRecency(items, 1);
  assert.equal(visible[0].id, "new");
});

test("custom tsOf getter (failed-lane groups)", () => {
  const groups = [
    { task: { id: "g1", updated_at: "2026-07-01" }, collapsedCount: 2 },
    { task: { id: "g2", updated_at: "2026-07-05" }, collapsedCount: 0 },
  ];
  const { visible, hiddenCount } = topByRecency(groups, 1, (g) => g.task.updated_at);
  assert.equal(visible[0].task.id, "g2");
  assert.equal(hiddenCount, 1);
});

// A cancelled task ends in `failed` status, so the Failed lane fills with cancels and a
// REAL failure — the only one that needs attention — gets buried behind "Show N more"
// purely because a cancel is more recent. Real failures always take the visible slots first.
test("topPrioritised surfaces real failures before cancelled ones", () => {
  const items = [
    { id: "c1", updated_at: "2026-07-13T10:00:00Z", cancelled: true },
    { id: "c2", updated_at: "2026-07-13T09:00:00Z", cancelled: true },
    { id: "f1", updated_at: "2026-07-13T01:00:00Z", cancelled: false }, // oldest, but REAL
  ];
  const isReal = (x) => !x.cancelled;
  const { visible, hiddenCount } = topPrioritised(items, 2, undefined, isReal);
  assert.deepEqual(visible.map((v) => v.id), ["f1", "c1"]);
  assert.equal(hiddenCount, 1);
});

test("topPrioritised keeps recency order within each group and is empty-safe", () => {
  const isReal = (x) => !x.cancelled;
  const items = [
    { id: "f_old", updated_at: "2026-07-10T00:00:00Z", cancelled: false },
    { id: "f_new", updated_at: "2026-07-13T00:00:00Z", cancelled: false },
    { id: "c", updated_at: "2026-07-14T00:00:00Z", cancelled: true },
  ];
  assert.deepEqual(
    topPrioritised(items, 3, undefined, isReal).visible.map((v) => v.id),
    ["f_new", "f_old", "c"],
  );
  assert.deepEqual(topPrioritised([], 4, undefined, isReal), { visible: [], hiddenCount: 0 });
  assert.deepEqual(topPrioritised(undefined, 4, undefined, isReal), { visible: [], hiddenCount: 0 });
});

test("topPrioritised with no predicate behaves exactly like topByRecency", () => {
  const items = [
    { id: "a", updated_at: "2026-07-11T00:00:00Z" },
    { id: "b", updated_at: "2026-07-13T00:00:00Z" },
    { id: "c", updated_at: "2026-07-12T00:00:00Z" },
  ];
  assert.deepEqual(topPrioritised(items, 2), topByRecency(items, 2));
});

// THE COMPOSITION THE APP ACTUALLY RUNS. The Failed lane feeds GROUPS (from
// groupFailedByTitle) into topPrioritised with a custom tsOf, and reads `cancelled` through
// `g.task`. Testing topPrioritised on bare tasks passed while the feature was dead on the
// only dataset it exists for: every group representative was a cancel, so the priority set
// was empty and the visible rows were byte-identical to plain recency.
test("failed lane: grouping + prioritising surfaces the real failure (the live shape)", async () => {
  const { groupFailedByTitle } = await import("./boardGroups.js");
  const { isRealFailure } = await import("./boardLanes.js");
  const failedLaneTasks = [
    // A cancel that is NEWER than the real failure it shares a title with.
    { id: "cancel_new", title: "Per-PR CI_GATE", status: "failed", cancelled: true, created_at: "2026-07-13T11:26:04Z", updated_at: "2026-07-13T11:26:04Z" },
    { id: "real_fail", title: "Per-PR CI_GATE", status: "failed", cancelled: false, created_at: "2026-07-13T11:05:57Z", updated_at: "2026-07-13T11:05:57Z" },
    ...Array.from({ length: 6 }, (_, i) => ({
      id: `other_cancel_${i}`, title: `Other ${i}`, status: "failed", cancelled: true,
      created_at: "2026-07-13T12:00:00Z", updated_at: "2026-07-13T12:00:00Z",
    })),
  ];
  const rows = groupFailedByTitle(failedLaneTasks);
  const tsOf = (g) => g.task.last_activity || g.task.updated_at || g.task.created_at || "";
  const { visible } = topPrioritised(rows, 4, tsOf, (g) => isRealFailure(g.task));

  const ids = visible.map((g) => g.task.id);
  assert.ok(ids.includes("real_fail"), `the real failure must be visible, got ${ids.join(",")}`);
  assert.equal(ids[0], "real_fail", "and it must lead the lane, ahead of newer cancels");
});
