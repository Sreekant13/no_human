import { test } from "node:test";
import assert from "node:assert/strict";
import { taskProgress } from "./taskProgress.js";

test("pipeline stages increase monotonically to 100", () => {
  const order = ["pending", "context", "planning", "implementing",
                 "reviewing", "testing", "awaiting_approval", "done"];
  const pcts = order.map(taskProgress);
  for (let i = 1; i < pcts.length; i++) assert.ok(pcts[i] > pcts[i - 1]);
  assert.equal(taskProgress("done"), 100);
  assert.ok(taskProgress("pending") > 0);
});

test("parked/terminal states have no misleading bar", () => {
  for (const s of ["blocked", "awaiting_input", "paused_quota", "escalated", "failed"])
    assert.equal(taskProgress(s), null);
  assert.equal(taskProgress("nonsense"), null);
});
