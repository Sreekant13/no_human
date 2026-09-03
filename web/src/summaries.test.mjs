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
    { ts: 1, kind: "models", text: "coder=claude-sonnet-5 · reviewer=claude-opus-5" },
    { ts: 2, kind: "attempt_start", text: "attempt 1/3" },
    { ts: 3, kind: "commit", text: "f800706b (1 files, +37/-4)" },
    { ts: 4, kind: "review", text: "FAIL", passed: false, blocking_count: 1 },
    { ts: 5, kind: "attempt_failed", text: "review failed: Outer 60m timeout undercuts inner waits" },
    { ts: 6, kind: "attempt_start", text: "attempt 2/3" },
    { ts: 7, kind: "review", text: "PASS", passed: true },
    { ts: 8, kind: "tests", text: "PASS: 627 passed, 0 failed" },
    { ts: 9, kind: "lifetime_budget", text: "attempts 20/23 · tokens 44,674,946/68,000,000" },
    { ts: 10, kind: "pr_open", text: "https://code.example.com/dev/metrics-core-query-service/pull/7004" },
  ]);
  assert.match(s.headline, /PR open/);
  assert.equal(s.facts.find(([l]) => l === "Attempts")[1], "2");
  assert.equal(s.facts.find(([l]) => l === "Reviews")[1], "2 (FP)");
  assert.match(s.issues[0].text, /Outer 60m timeout/);
  // Found on attempt 1, then attempt 2 passed and a PR opened — resolved, not open.
  assert.equal(s.issues[0].resolved, true);
  assert.match(s.highlights[0], /^Now: /);
});

test("task digest: a finding with no later success stays open (red)", () => {
  const s = taskSummary([
    { ts: 1, kind: "attempt_start", text: "attempt 1/3" },
    { ts: 2, kind: "review", text: "FAIL", passed: false, blocking_count: 1 },
    { ts: 3, kind: "attempt_failed", text: "review failed: null deref in parser" },
    { ts: 4, kind: "escalated", text: "**Category:** budget" },
  ]);
  const finding = s.issues.find((i) => i.text.includes("null deref"));
  assert.ok(finding, "expected the finding to be listed");
  assert.equal(finding.resolved, false);
  // The blocker line is likewise still open.
  const blocker = s.issues.find((i) => i.text.startsWith("Last blocker:"));
  assert.equal(blocker.resolved, false);
});

test("task digest: an earlier finding fixed, a later one still open", () => {
  const s = taskSummary([
    { ts: 1, kind: "attempt_start", text: "attempt 1/3" },
    { ts: 2, kind: "attempt_failed", text: "review failed: finding A" },
    { ts: 3, kind: "attempt_start", text: "attempt 2/3" },
    { ts: 4, kind: "review", text: "PASS", passed: true },
    { ts: 5, kind: "pr_open", text: "https://example.com/pr/1" },
    // A round-2 review reopens something, and no later success follows it.
    { ts: 6, kind: "attempt_failed", text: "review failed: finding B" },
  ]);
  const a = s.issues.find((i) => i.text.includes("finding A"));
  const b = s.issues.find((i) => i.text.includes("finding B"));
  assert.equal(a.resolved, true);
  assert.equal(b.resolved, false);
});

test("empty events yield null, not a crash", () => {
  assert.equal(taskSummary([]), null);
  assert.equal(agentSummary([], CODER), null);
});

test("watcher digest: CI_GATE verdicts and shepherding state", () => {
  const evs = [
    { kind: "ci_gate_trigger", source: "watcher", text: "pipeline 7008", ts: 1 },
    { kind: "ci_gate_poll", source: "watcher", text: "running", ts: 2 },
    { kind: "ci_gate_pass", source: "watcher", text: "PASSED (PR comment posted)", ts: 3 },
  ];
  const s = agentSummary(evs, { id: "watcher" });
  assert.ok(s.headline.includes("shepherding"));
  assert.ok(s.facts.some(([k, v]) => k === "CI_GATE" && v.includes("1 passed")));
  assert.equal(s.issues.length, 0);
});

test("task digest carries the CI_GATE verdict", () => {
  const evs = [
    { kind: "attempt_start", ts: 1, text: "" },
    { kind: "ci_gate_pass", source: "watcher", ts: 2, text: "PASSED" },
  ];
  const s = taskSummary(evs);
  assert.ok(s.facts.some(([k, v]) => k === "CI_GATE" && v === "integration passed"));
});

// A four-role production-shaped models string — long enough that an appended
// suffix (the old disclosure mechanism) could never render past the 110-char
// clip on the "Models" fact. The reviewer backend must instead surface as its
// own, un-clipped digest fact sourced from the `role_backends` event kwarg.
const PROD_MODELS_TEXT =
  "coder=claude-sonnet-5 · planner=claude-opus-4-8 · reviewer=claude-opus-4-8 · "
  + "supervisor=claude-sonnet-5 · auth=personal";

test("task digest: a non-default reviewer backend renders as its own fact past the 110-char models clip", () => {
  assert.ok(PROD_MODELS_TEXT.length > 110);
  const s = taskSummary([
    { ts: 1, kind: "attempt_start", text: "attempt 1/3" },
    {
      ts: 2, kind: "models", text: PROD_MODELS_TEXT,
      role_backends: { reviewer: { backend: "codex", model: "gpt-5-codex" } },
    },
  ]);
  const modelsFact = s.facts.find(([l]) => l === "Models")[1];
  assert.ok(modelsFact.endsWith("…"));
  const reviewerFact = s.facts.find(([l]) => l === "Reviewer backend")[1];
  assert.match(reviewerFact, /codex/);
  assert.match(reviewerFact, /chosen in Settings/);
  assert.equal(s.non_default_reviewer.non_default_reviewer, "codex");
});

test("task digest: a default reviewer adds no reviewer-backend fact", () => {
  const s = taskSummary([
    { ts: 1, kind: "attempt_start", text: "attempt 1/3" },
    { ts: 2, kind: "models", text: PROD_MODELS_TEXT },
  ]);
  assert.equal(s.facts.find(([l]) => l === "Reviewer backend"), undefined);
  assert.equal(s.non_default_reviewer, null);
});
