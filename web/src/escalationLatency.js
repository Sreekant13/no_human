// The board card's escalation-latency line: how long it took the run to reach
// a human, in the two units that matter — attempts and tokens.
//
// Distinct from cardBlockerLine.js (which reads the blocker's own PROSE): this
// reads the structured `escalation_latency` numbers the orchestrator stamps
// from `store.lifetime_usage` (`_raise_blocker`), so it never depends on the
// blocker template matching.

import { fmtTokens } from "./cost.js";

/**
 * @param {object|null|undefined} task a task summary (needs `status`,
 *   `escalation_attempts`, `escalation_tokens`)
 * @returns {string|null} "Attempted N times, M tokens", or null when the task
 *   is not escalated or the numbers were never measured (unmeasured != zero —
 *   never fabricate a "0 tokens" line).
 */
export function escalationLatencyLine(task) {
  if (!task || task.status !== "escalated") return null;
  const a = task.escalation_attempts;
  const t = task.escalation_tokens;
  if (!Number.isFinite(a) || !Number.isFinite(t)) return null;
  return `Attempted ${a} time${a === 1 ? "" : "s"}, ${fmtTokens(t)} tokens`;
}
