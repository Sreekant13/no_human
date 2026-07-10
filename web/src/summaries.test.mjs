import test from "node:test";
import assert from "node:assert/strict";

import { agentSummary, taskSummary } from "./summaries.js";

const CODER = { id: "agent" };

test("coder digest: files touched, commands, latest result", () => {
  const s = agentSummary([
    { ts: 1, source: "agent", kind: "tool_use", tool_name: "Read", tool_input: { file_path: "/r/Jenkinsfile" } },
    { ts: 2, source: "agent", kind: "tool_use", tool_name: "Bash", tool_input: { command: "run tests" } },
    { ts: 3, source: "agent", kind: "tool_use", tool_name: "Edit", tool_input: { file_path: "/r/Jenkinsfile" } },
    { ts: 4, source: "agent", kind: "tool_use", tool_name: "Edit", tool_input: { file_path: "/r/Jenkinsfile" } },
    { ts: 99, source: "agent", kind: "result", text: "Fixed the reviewer's cited finding: image pinning." },
  ], CODER);
  assert.match(s.headline, /1 file\(s\) touched/);
  assert.deepEqual(s.facts.find(([l]) => l === "Files edited")[1], "Jenkinsfile ×2");
  assert.match(s.highlights[0], /image pinning/);
});

test("supervisor digest: corrections carry their messages", () => {
  const s = agentSummary([
    { ts: 1, source: "orchestrator", kind: "supervisor_decision", text: "continue", message: "on track" },
    { ts: 2, source: "orchestrator", kind: "supervisor_decision", text: "correct",
      message: "Use the no_human_verify skill instead of hand-crafting groovy invocations" },
  ], { id: "supervisor" });
  assert.match(s.headline, /Course-corrected the coder 1×/);
  assert.match(s.highlights[0], /no_human_verify/);
});

test("supervisor digest: older feeds without message say so instead of showing nothing", () => {
  const s = agentSummary([
    { ts: 1, source: "orchestrator", kind: "supervisor_decision", text: "correct" },
  ], { id: "supervisor" });
  assert.equal(s.highlights.length, 0);
  assert.match(s.issues[0], /not in this feed/);
});

test("reviewer digest: verdict chain with severity counts", () => {
  const s = agentSummary([
    { ts: 1, source: "orchestrator", kind: "review", text: "FAIL", passed: false, blocking_count: 2, advisory_count: 1 },
    { ts: 2, source: "orchestrator", kind: "review", text: "PASS", passed: true, blocking_count: 0, advisory_count: 3 },
  ], { id: "reviewer" });
  assert.match(s.headline, /2 review round\(s\): FAIL \(2 blocking\) → PASS \(\+3 advisory\)/);
});

test("subagent digest: last progress and completion", () => {
  const s = agentSummary([
    { ts: 1, kind: "subagent_start", task_id: "a1", source: "planner:risk-first", text: "Investigate Jenkinsfile" },
    { ts: 5, kind: "subagent_progress", task_id: "a1", text: "found the lock block at 641" },
    { ts: 9, kind: "subagent_done", task_id: "a1", status: "completed", text: "report: structure confirmed" },
  ], { id: "sub-a1", label: "Investigate Jenkinsfile", subagentTaskId: "a1" });
  assert.match(s.headline, /finished/);
  assert.match(s.highlights[0], /lock block/);
  assert.match(s.highlights[1], /structure confirmed/);
});

test("task digest: attempts, verdict chain, PR, budget, current line", () => {
  const s = taskSummary([
    { ts: 1, kind: "models", text: "coder=claude-sonnet-5 · reviewer=claude-opus-4-8" },
    { ts: 2, kind: "attempt_start", text: "attempt 1/3" },
    { ts: 3, kind: "commit", text: "f800706b (1 files, +37/-4)" },
    { ts: 4, kind: "review", text: "FAIL", passed: false, blocking_count: 1 },
    { ts: 5, kind: "attempt_failed", text: "review failed: Outer 60m timeout undercuts inner waits" },
    { ts: 6, kind: "attempt_start", text: "attempt 2/3" },
    { ts: 7, kind: "review", text: "PASS", passed: true },
    { ts: 8, kind: "tests", text: "PASS: 627 passed, 0 failed" },
    { ts: 9, kind: "lifetime_budget", text: "attempts 20/23 · tokens 44,674,946/68,000,000" },
    { ts: 10, kind: "pr_open", text: "https://code.example.com/dev/metrics-core-query-service/pull/531" },
  ]);
  assert.match(s.headline, /PR open/);
  assert.equal(s.facts.find(([l]) => l === "Attempts")[1], "2");
  assert.equal(s.facts.find(([l]) => l === "Reviews")[1], "2 (FP)");
  assert.match(s.issues[0], /Outer 60m timeout/);
  assert.match(s.highlights[0], /^Now: /);
});

test("empty events yield null, not a crash", () => {
  assert.equal(taskSummary([]), null);
  assert.equal(agentSummary([], CODER), null);
});
