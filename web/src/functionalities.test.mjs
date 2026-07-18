import test from "node:test";
import assert from "node:assert/strict";
import {
  FUNCTIONALITIES, FX_OF_ROLE, currentFunctionality, groupFunctionalities,
  clampAgentState, laneAgentRows,
} from "./functionalities.js";
import { eventSource } from "./eventRoles.js";

const idle = { status: "idle", count: 0, lastText: "" };

test("every role in FX_OF_ROLE maps to a declared functionality", () => {
  const ids = new Set(FUNCTIONALITIES.map((f) => f.id));
  for (const fx of Object.values(FX_OF_ROLE)) assert.ok(ids.has(fx), fx);
  // And every functionality's roles map back to it — no orphan roles.
  for (const f of FUNCTIONALITIES) {
    for (const r of f.roles) assert.equal(FX_OF_ROLE[r], f.id);
  }
});

test("currentFunctionality follows the last event's role; supervisor lights Coding", () => {
  const evs = [
    { kind: "attempt_start", source: "orchestrator", text: "a1" },
    { kind: "supervisor_decision", source: "orchestrator", text: "correct" },
  ];
  assert.equal(currentFunctionality(evs, true), "coding");
  assert.equal(currentFunctionality(evs, false), null, "inactive task has no current stage");
  assert.equal(currentFunctionality([], true), null);
});

test("groupFunctionalities: counts, presence, model, and freshest text per stage", () => {
  const agentStates = {
    worker: { status: "active", count: 10, lastText: "attempt 2" },
    planner: { status: "done", count: 4, lastText: "plan generated" },
    agent: { status: "active", count: 30, lastText: "editing foo.py" },
    supervisor: { status: "active", count: 3, lastText: "correct" },
    reviewer: idle,
    "sub-t1": { status: "done", count: 5, lastText: "researched" },
  };
  const subagents = [{ id: "sub-t1", parent: "planner", status: "done" }];
  const events = [
    { kind: "planning", source: "planner", text: "plan generated" },
    { kind: "tool_use", source: "agent", text: "Edit foo.py" },
    { kind: "supervisor_decision", source: "orchestrator", text: "on track" },
  ];
  const groups = groupFunctionalities({
    agentStates, subagents, models: { agent: "claude-sonnet-5" }, events,
  });
  const byId = Object.fromEntries(groups.map((g) => [g.id, g]));

  assert.equal(byId.planning.agentCount, 2, "planner + its subagent");
  // The subagent's 5 events already live inside the planner's role count
  // (subagent events carry the spawning role's source) — no double count.
  assert.equal(byId.planning.eventCount, 4);
  assert.equal(byId.coding.agentCount, 2, "coder + supervisor");
  assert.equal(byId.coding.model, "claude-sonnet-5");
  assert.equal(byId.coding.lastText, "on track", "supervisor's event is the stage's freshest");
  assert.equal(byId.reviewing.present, false);
  assert.equal(byId.reviewing.status, "idle");
});

test("status precedence: error beats active beats done", () => {
  const mk = (worker) => groupFunctionalities({
    agentStates: { worker, planner: idle, agent: idle, supervisor: idle, reviewer: idle },
    subagents: [], events: [],
  })[0].status;
  assert.equal(mk({ status: "error", count: 1, lastText: "" }), "error");
  assert.equal(mk({ status: "active", count: 1, lastText: "" }), "active");
  assert.equal(mk({ status: "done", count: 1, lastText: "" }), "done");
});

test("a subagent with an unknown parent lands in Orchestrating, not nowhere", () => {
  const groups = groupFunctionalities({
    agentStates: { worker: { status: "active", count: 2, lastText: "" } },
    subagents: [{ id: "sub-x", parent: "mystery", status: "active" }],
    events: [],
  });
  const orch = groups.find((g) => g.id === "orchestrating");
  assert.equal(orch.subs.length, 1);
  assert.equal(orch.status, "active");
});

test("watcher events land in the Shepherding stage, not Orchestrating", () => {
  const ci_gateEvent = { kind: "ci_gate_pass", source: "watcher", text: "PASSED" };
  assert.equal(FX_OF_ROLE[eventSource(ci_gateEvent)], "shepherding");
  assert.ok(FUNCTIONALITIES.some((f) => f.id === "shepherding"));
});

// laneAgentRows turns a grouped stage into an ordered agent tree for the
// SYSTEM tab: the primary agent as a depth-0 row, its additional agents (the
// coding Supervisor, any discovered subagents) as depth-1 children, with the
// last child flagged so the tree connector can terminate its rail.
function group(id) {
  return (states, subs = []) => groupFunctionalities({
    agentStates: states, subagents: subs, events: [],
  }).find((g) => g.id === id);
}

test("laneAgentRows: single-agent stage is one depth-0 row, no children", () => {
  const g = group("orchestrating")({ worker: { status: "active", count: 3, lastText: "" } });
  const rows = laneAgentRows(g);
  assert.deepEqual(rows, [{ kind: "role", id: "worker", depth: 0, isLastChild: false }]);
});

test("laneAgentRows: coding nests the Supervisor as a child under the Coder", () => {
  const g = group("coding")({
    agent: { status: "active", count: 30, lastText: "" },
    supervisor: { status: "active", count: 3, lastText: "" },
  });
  const rows = laneAgentRows(g);
  assert.equal(rows.length, 2);
  assert.deepEqual(rows[0], { kind: "role", id: "agent", depth: 0, isLastChild: false });
  assert.deepEqual(rows[1], { kind: "role", id: "supervisor", depth: 1, isLastChild: true });
});

test("laneAgentRows: planner subagents are depth-1 children, last one flagged", () => {
  const subs = [
    { id: "sub-a", parent: "planner", status: "done", label: "Find guard.py seam" },
    { id: "sub-b", parent: "planner", status: "done", label: "Find config seam" },
  ];
  const g = group("planning")(
    { planner: { status: "done", count: 4, lastText: "" },
      "sub-a": { status: "done", count: 2, lastText: "" },
      "sub-b": { status: "done", count: 2, lastText: "" } },
    subs);
  const rows = laneAgentRows(g);
  assert.equal(rows.length, 3);
  assert.deepEqual(rows[0], { kind: "role", id: "planner", depth: 0, isLastChild: false });
  assert.equal(rows[1].kind, "sub");
  assert.equal(rows[1].id, "sub-a");
  assert.equal(rows[1].depth, 1);
  assert.equal(rows[1].isLastChild, false);
  assert.equal(rows[1].sub.label, "Find guard.py seam");
  assert.equal(rows[2].id, "sub-b");
  assert.equal(rows[2].isLastChild, true);
});

test("clampAgentState: stale active reads done on a non-running task", () => {
  assert.deepEqual(
    clampAgentState({ status: "active", count: 5, lastText: "x" }, false),
    { status: "done", count: 5, lastText: "x" });
  // running task: untouched
  assert.equal(
    clampAgentState({ status: "active", count: 5, lastText: "x" }, true).status,
    "active");
  // errors are never masked
  assert.equal(
    clampAgentState({ status: "error", count: 1, lastText: "" }, false).status,
    "error");
});
