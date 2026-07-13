import { test } from "node:test";
import assert from "node:assert/strict";
import { topByRecency } from "./laneView.js";

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
