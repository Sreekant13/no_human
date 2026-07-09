// Run with: node --test web/src/
import test from "node:test";
import assert from "node:assert/strict";

import { ROLE_LABEL, discoverSubagents, eventLens, eventSource, modelsByNode } from "./eventRoles.js";

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

test("each node is labelled with the model that actually ran it", () => {
  // Exactly the map the orchestrator emitted on run 61406d02.
  const models = modelsByNode([
    { kind: "state", text: "implementing" },
    { kind: "models", models: { coder: "claude-sonnet-5", planner: "claude-opus-4-8",
                                reviewer: "claude-opus-4-8",
                                supervisor: "claude-sonnet-5" } },
  ]);
  assert.deepEqual(models, {
    agent: "claude-sonnet-5",
    planner: "claude-opus-4-8",
    reviewer: "claude-opus-4-8",
    supervisor: "claude-sonnet-5",
  });
});

test("every role the orchestrator emits maps to a real node", () => {
  // A role with no node silently loses its model label — which is how the
  // Supervisor ended up as the only unlabelled node right after its tier
  // changed. Keep this in step with Orchestrator._active_models().
  const emitted = ["coder", "planner", "reviewer", "supervisor"];
  const mapped = modelsByNode([
    { kind: "models", models: Object.fromEntries(emitted.map((r) => [r, "m"])) },
  ]);
  assert.equal(Object.keys(mapped).length, emitted.length);
});

test("a later attempt's models win, and events without them are ignored", () => {
  assert.deepEqual(modelsByNode([]), {});
  assert.deepEqual(modelsByNode([{ kind: "models" }]), {});
  const models = modelsByNode([
    { kind: "models", models: { coder: "old-model" } },
    { kind: "models", models: { coder: "claude-sonnet-5" } },
  ]);
  assert.equal(models.agent, "claude-sonnet-5");
});

test("an unknown role never invents a node", () => {
  assert.deepEqual(modelsByNode([{ kind: "models", models: { aggregator: "x" } }]), {});
  assert.deepEqual(modelsByNode([{ kind: "models", models: { distiller: "x" } }]), {});
});

test("a backgrounded shell command is not a subagent", () => {
  // The SDK reports background Bash with the same events as an Agent dispatch,
  // distinguished only by task_type. Run 61406d02 put 14 bash one-liners on the
  // Coder node as though it had spawned 14 agents.
  const subs = discoverSubagents([
    { kind: "subagent_start", source: "agent", task_id: "bts1nfycu",
      task_type: "local_bash",
      text: "source .venv312/bin/activate && python run_tests.py 2>&1 | tail -80" },
    { kind: "subagent_start", source: "planner:minimal-first", task_id: "a92dcf7c",
      task_type: "local_agent", text: "Investigate Jenkinsfile structure" },
    { kind: "subagent_done", task_id: "bts1nfycu", status: "completed" },
  ]);
  assert.equal(subs.length, 1);
  assert.equal(subs[0].label, "Investigate Jenkinsfile structure");
  assert.equal(subs[0].parent, "planner");
});

test("an unknown task_type is still shown (visible beats hidden)", () => {
  const subs = discoverSubagents([
    { kind: "subagent_start", source: "agent", task_id: "x", text: "future thing" },
    { kind: "subagent_start", source: "agent", task_id: "y", task_type: "local_agent", text: "z" },
  ]);
  assert.equal(subs.length, 2);
});

test("the Orchestrator does not take credit for the roles it emits for", () => {
  // Run 61406d02 emitted 18 supervisor events via self.emit(), all carrying
  // source:"orchestrator" — so the Supervisor node never rendered at all.
  assert.equal(eventSource({ source: "orchestrator", kind: "supervisor_decision" }), "supervisor");
  assert.equal(eventSource({ source: "orchestrator", kind: "supervisor" }), "supervisor");
  assert.equal(eventSource({ source: "orchestrator", kind: "review_error" }), "reviewer");
  assert.equal(eventSource({ source: "orchestrator", kind: "tamper" }), "reviewer");
  // Its own events still belong to it.
  assert.equal(eventSource({ source: "orchestrator", kind: "state" }), "worker");
  assert.equal(eventSource({ source: "orchestrator", kind: "commit" }), "worker");
  assert.equal(eventSource({ source: "orchestrator", kind: "stuck" }), "worker");
});
