// The night ledger: what no_human did while the operator was away.
// Pure derivation over the board's task list — same source as every lane,
// same predicates (isNeedsYou / isRealFailure from boardLanes) — so it can
// never contradict the board (the M1/M2 lesson: two surfaces, two truths).
import { costOf } from "./cost.js";
import { isNeedsYou, isRealFailure } from "./boardLanes.js";

export const LEDGER_WINDOW_MS = 24 * 60 * 60 * 1000;

// The WHOLE run: coder + review + aux buckets — the nine TaskSummaryOut
// fields the list endpoint sends precisely so a row prices the full pipeline
// (models.py B2 #12/#5). PR #104 review: the first draft read attempt-level
// names that don't exist on tasks and priced every night at $0.
function taskCost(t) {
  return costOf({
    used: (t.total_tokens || 0) + (t.total_review_tokens || 0)
      + (t.total_aux_tokens || 0),
    creation: (t.total_cache_creation || 0)
      + (t.total_review_cache_creation || 0) + (t.total_aux_cache_creation || 0),
    read: (t.total_cache_read || 0) + (t.total_review_cache_read || 0)
      + (t.total_aux_cache_read || 0),
  });
}

export function ledgerSummary(tasks, now = Date.now(), windowMs = LEDGER_WINDOW_MS) {
  const since = now - windowMs;
  // No upper bound: a timestamp milliseconds ahead (clock skew) still counts.
  const inWindow = (t) =>
    new Date(t.updated_at || t.created_at || 0).getTime() >= since;
  const recent = (tasks || []).filter(inWindow);
  const done = recent.filter((t) => t.status === "done").length;
  const failed = recent.filter(isRealFailure).length;
  const parked = recent.filter(isNeedsYou).length;
  const cost = recent.reduce((sum, t) => sum + taskCost(t), 0);
  // "Quiet" must never hide spend: a night where an in-flight task burned
  // real tokens is not quiet, even with zero terminal events.
  return { done, failed, parked, cost,
           quiet: done + failed + parked === 0 && cost === 0 };
}
