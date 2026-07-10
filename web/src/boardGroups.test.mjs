import test from "node:test";
import assert from "node:assert/strict";
import { groupFailedByTitle } from "./boardGroups.js";

test("same-title failed cards collapse to the newest with a count", () => {
  const tasks = [
    { id: "a", title: "Per-PR CI_GATE", created_at: "2026-07-08" },
    { id: "b", title: "Per-PR CI_GATE", created_at: "2026-07-10" },
    { id: "c", title: "Per-PR CI_GATE", created_at: "2026-07-09" },
    { id: "d", title: "Other bug", created_at: "2026-07-10" },
  ];
  const groups = groupFailedByTitle(tasks);
  assert.equal(groups.length, 2);
  const ci_gate = groups.find((g) => g.task.id === "b");
  assert.equal(ci_gate.collapsedCount, 2);
  assert.deepEqual(ci_gate.olderIds, ["c", "a"]);  // newest-first
  const other = groups.find((g) => g.task.id === "d");
  assert.equal(other.collapsedCount, 0);
});

test("untitled tasks never collapse into each other", () => {
  const groups = groupFailedByTitle([
    { id: "x", title: "", created_at: "1" },
    { id: "y", title: "", created_at: "2" },
  ]);
  assert.equal(groups.length, 2);
});
