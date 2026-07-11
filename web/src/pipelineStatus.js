import { eventSource } from "./eventRoles.js";

// Derives a pipeline node's live status (idle / active / done / error) from the
// task's event stream. Extracted from SlideOver.jsx so the "is this node in
// error?" rule — which is subtle and was getting it wrong — is node-testable.

export const ERROR_KINDS = new Set([
  "failed", "attempt_failed", "agent_error", "review_error",
]);

// A failure OLDER than any of these means the pipeline moved PAST it — the
// failure is history, not the present. Without this, a single failed attempt
// painted the Orchestrator node red ERROR forever, so a task that is actually
// green/merge-ready, retrying, or simply parked read as broken. Recovery =
//   - a positive milestone (pr_open / review pass / ci_gate pass / merged), OR
//   - forward progress: a NEW attempt's state (planning/implementing/testing)
//     or a settled non-error state (awaiting_approval/done) after the error, OR
//   - `cancelled`: an operator stop is a deliberate park, never an error.
// Deliberately NOT recovery: state=failed / escalated / blocked (those ARE the
// present, so a real terminal failure still shows error).
export const isRecovery = (e) =>
  e.kind === "pr_open" || e.kind === "ci_gate_pass" || e.kind === "merged"
  || e.kind === "cancelled"
  || (e.kind === "review" && /pass/i.test(e.text || ""))
  || (e.kind === "state"
      && /implementing|planning|testing|awaiting_approval|done/i.test(e.text || ""));

export function deriveAgentStatus(events, agentId) {
  const agentEvents = events.filter((e) => eventSource(e) === agentId);
  if (agentEvents.length === 0) return { status: "idle", count: 0, lastText: "" };
  const last = agentEvents[agentEvents.length - 1];
  const lastErrorTs = agentEvents.reduce(
    (m, e) => (ERROR_KINDS.has(e.kind) && (e.ts || 0) > m ? e.ts : m), 0);
  const lastRecoveryTs = events.reduce(
    (m, e) => (isRecovery(e) && (e.ts || 0) > m ? e.ts : m), 0);
  const hasDone = events.some((e) => e.kind === "state" && /done/i.test(e.text));
  const hasReviewPass = agentEvents.some(
    (e) => e.kind === "review" && /pass/i.test(e.text));
  let status = "active";
  if (hasDone || hasReviewPass) status = "done";
  if (lastErrorTs > 0 && lastErrorTs > lastRecoveryTs && !hasDone) status = "error";
  return { status, count: agentEvents.length, lastText: last.text || "" };
}
