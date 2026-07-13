import { test } from "node:test";
import assert from "node:assert/strict";
import { kindLabel, groupByTask } from "./searchView.js";

test("kindLabel maps known kinds and falls back to the raw kind", () => {
  assert.equal(kindLabel("attempt_failed"), "failed attempt");
  assert.equal(kindLabel("tamper"), "tamper guard");
  assert.equal(kindLabel("weird_kind"), "weird_kind");
  assert.equal(kindLabel(""), "event");
  assert.equal(kindLabel(undefined), "event");
});

test("groupByTask groups hits by task, preserving rank order", () => {
  const groups = groupByTask([
    { task_id: "aaa", task_title: "A", kind: "review", snippet: "x" },
    { task_id: "bbb", task_title: "B", kind: "tamper", snippet: "y" },
    { task_id: "aaa", task_title: "A", kind: "blocked", snippet: "z" },
  ]);
  assert.deepEqual(groups.map(g => g.task_id), ["aaa", "bbb"]);
  assert.equal(groups[0].hits.length, 2);
  assert.equal(groups[1].hits.length, 1);
});

test("groupByTask on empty/undefined is an empty array", () => {
  assert.deepEqual(groupByTask([]), []);
  assert.deepEqual(groupByTask(undefined), []);
});
