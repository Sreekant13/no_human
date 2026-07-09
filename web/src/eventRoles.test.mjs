// Run with: node --test web/src/
import test from "node:test";
import assert from "node:assert/strict";

import { ROLE_LABEL, discoverSubagents, eventLens, eventSource } from "./eventRoles.js";

test("planner lenses and the aggregator fold onto one Planner node", () => {
  assert.equal(eventSource({ source: "planner" }), "planner");
  assert.equal(eventSource({ source: "planner:minimal-first" }), "planner");
  assert.equal(eventSource({ source: "planner:risk-first" }), "planner");
  assert.equal(eventSource({ source: "aggregator" }), "planner");
});

test("the other roles are untouched", () => {
  assert.equal(eventSource({ source: "agent" }), "agent");
  assert.equal(eventSource({ source: "reviewer" }), "reviewer");
  assert.equal(eventSource({ source: "supervisor" }), "supervisor");
  // The Orchestrator node's id is "worker", not "orchestrator".
  assert.equal(eventSource({ source: "orchestrator" }), "worker");
});

test("events with no source still fall back to the kind map", () => {
  // Every event persisted before role stamping existed looks like this.
  assert.equal(eventSource({ kind: "tool_use" }), "agent");
  assert.equal(eventSource({ kind: "subagent_start" }), "agent");
  assert.equal(eventSource({ kind: "review" }), "reviewer");
  assert.equal(eventSource({ kind: "something_new" }), "worker");
});

test("the Planner node has a label (an unlabelled node renders blank)", () => {
  for (const id of ["worker", "planner", "supervisor", "reviewer", "agent"]) {
    assert.ok(ROLE_LABEL[id], `no label for ${id}`);
  }
});

test("eventLens extracts the lens, and only from a lensed planner", () => {
  assert.equal(eventLens({ source: "planner:test-first" }), "test-first");
  assert.equal(eventLens({ source: "planner" }), null);
  assert.equal(eventLens({ source: "aggregator" }), null);
  assert.equal(eventLens({ source: "agent" }), null);
  assert.equal(eventLens({}), null);
});

test("subagents are attributed to the role that spawned them", () => {
  // Two proposers each spawned a subagent with the SAME label — that really
  // happened in run 0305e5ce, and both rendered under Coder.
  const subs = discoverSubagents([
    { kind: "subagent_start", source: "planner:minimal-first",
      task_id: "sdk-1", text: "Investigate Jenkinsfile structure" },
    { kind: "subagent_start", source: "planner:test-first",
      task_id: "sdk-2", text: "Investigate Jenkinsfile structure" },
    { kind: "subagent_start", source: "agent",
      task_id: "sdk-3", text: "Write the parser" },
    { kind: "subagent_done", task_id: "sdk-1", status: "completed" },
    { kind: "subagent_done", task_id: "sdk-3", status: "failed" },
  ]);

  assert.equal(subs.length, 3, "distinct dispatches must stay distinct nodes");

  const byId = Object.fromEntries(subs.map((s) => [s.subagentTaskId, s]));
  assert.equal(byId["sdk-1"].parent, "planner");
  assert.equal(byId["sdk-2"].parent, "planner");
  assert.equal(byId["sdk-3"].parent, "agent");

  // The lens disambiguates two identically-named planner subagents.
  assert.equal(byId["sdk-1"].lens, "minimal-first");
  assert.equal(byId["sdk-2"].lens, "test-first");
  assert.match(byId["sdk-1"].desc, /minimal-first proposer/);
  assert.match(byId["sdk-3"].desc, /spawned by Coder/);

  // Colour follows the parent role, so planner children don't look like coder work.
  assert.equal(byId["sdk-1"].color, "var(--agent-planner)");
  assert.equal(byId["sdk-3"].color, "var(--agent-agent)");

  assert.equal(byId["sdk-1"].status, "done");
  assert.equal(byId["sdk-2"].status, "active");
  assert.equal(byId["sdk-3"].status, "error");
});

test("the render split puts planner subagents under the Planner, not the Coder", () => {
  const subs = discoverSubagents([
    { kind: "subagent_start", source: "planner:risk-first", task_id: "a", text: "x" },
    { kind: "subagent_start", source: "agent", task_id: "b", text: "y" },
  ]);
  // This is exactly what SystemView does to decide which row each one lands in.
  const plannerSubs = subs.filter((s) => s.parent === "planner");
  const coderSubs = subs.filter((s) => s.parent !== "planner");
  assert.deepEqual(plannerSubs.map((s) => s.subagentTaskId), ["a"]);
  assert.deepEqual(coderSubs.map((s) => s.subagentTaskId), ["b"]);
});

test("old persisted events (source:'agent' on everything) still render", () => {
  // Backwards compatibility: run 0305e5ce's 160 events all say source:"agent".
  const subs = discoverSubagents([
    { kind: "subagent_start", source: "agent", task_id: "old-1", text: "z" },
  ]);
  assert.equal(subs[0].parent, "agent");
  assert.equal(subs[0].lens, null);
});
