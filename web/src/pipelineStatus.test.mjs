import { test } from "node:test";
import assert from "node:assert/strict";
import { deriveAgentStatus, isRecovery } from "./pipelineStatus.js";

// eventSource maps events to a node id; orchestrator events use source "worker"
// or "orchestrator" per eventRoles.js. Build events with an explicit source the
// mapper recognises as the orchestrator node ("worker").
const ev = (kind, text, ts, source = "worker") => ({ kind, text, ts, source });

test("idle when the node has no events", () => {
  const s = deriveAgentStatus([ev("state", "planning", 1, "planner")], "worker");
  assert.equal(s.status, "idle");
  assert.equal(s.count, 0);
});

test("a fresh failure with no recovery shows error", () => {
  const events = [
    ev("state", "implementing", 1),
    ev("attempt_failed", "tests failed", 2),
  ];
  assert.equal(deriveAgentStatus(events, "worker").status, "error");
});

test("REGRESSION: operator-cancelled after a stale failure is NOT error", () => {
  // The live 537a1535 chain: one attempt_failed (the overnight timeout), the
  // pipeline continued, then the operator stopped it. Status is 'blocked' but
  // the node used to show a red ERROR from the stale failure.
  const events = [
    ev("attempt_failed", "tests failed: 0 passed, 0 failed, 1 errors", 10),
    ev("state", "implementing", 11),
    ev("state", "reviewing", 12),
    ev("cancelled", "stopped by operator: core pipeline PROVEN", 13),
  ];
  assert.notEqual(deriveAgentStatus(events, "worker").status, "error");
});

test("forward progress (a new attempt) after a failure clears the error", () => {
  const events = [
    ev("attempt_failed", "tests failed", 5),
    ev("state", "implementing", 6), // retry started → not currently erroring
  ];
  assert.notEqual(deriveAgentStatus(events, "worker").status, "error");
});

test("a genuine terminal failure still shows error", () => {
  const events = [
    ev("state", "implementing", 1),
    ev("attempt_failed", "tests failed", 2),
    ev("state", "failed", 3), // failed is NOT recovery
  ];
  assert.equal(deriveAgentStatus(events, "worker").status, "error");
});

test("a passed review marks the node done", () => {
  const events = [ev("review", "PASS: all checks", 4)];
  assert.equal(deriveAgentStatus(events, "worker").status, "done");
});

test("isRecovery: cancelled and progress states count; blocked/failed do not", () => {
  assert.ok(isRecovery({ kind: "cancelled", text: "stopped by operator" }));
  assert.ok(isRecovery({ kind: "pr_open", text: "" }));
  assert.ok(isRecovery({ kind: "state", text: "implementing" }));
  assert.ok(isRecovery({ kind: "state", text: "awaiting_approval" }));
  assert.equal(isRecovery({ kind: "state", text: "blocked" }), false);
  assert.equal(isRecovery({ kind: "state", text: "escalated" }), false);
  assert.equal(isRecovery({ kind: "attempt_failed", text: "" }), false);
});
