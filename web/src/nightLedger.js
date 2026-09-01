// The night ledger: what no_human did while the operator was away.
// Counts (done/failed/parked) are a pure derivation over the board's task
// list — same source as every lane, same predicates (isNeedsYou /
// isRealFailure from boardLanes) — so they can never contradict the board
// (the M1/M2 lesson: two surfaces, two truths).
//
// Spend is NOT derived here. It used to be: this file filtered `tasks` by
// `updated_at` and summed each survivor's LIFETIME `cost_usd` — so closing or
// cancelling an old task (bumping only `updated_at`, no new spend) swept its
// whole historical cost into "last 24h" (measured ~3.5x inflation on a live
// board). The server now prices only attempts whose OWN activity falls in
// the window (`core/metrics.py:window_spend`, served at
// `/api/metrics/window`) and this file just relays that figure — `spend` is
// the caller's already-fetched `{cost_usd, tokens, ...}` (or null before the
// first fetch / on an old server).
import { isNeedsYou, isRealFailure } from "./boardLanes.js";
import { timestampMs } from "./parseTimestamp.js";

export const LEDGER_WINDOW_MS = 24 * 60 * 60 * 1000;

export function ledgerSummary(tasks, now = Date.now(), windowMs = LEDGER_WINDOW_MS, spend = null) {
  const since = now - windowMs;
  // No upper bound: a timestamp milliseconds ahead (clock skew) still counts.
  const inWindow = (t) =>
    timestampMs(t.updated_at || t.created_at, 0) >= since;
  const recent = (tasks || []).filter(inWindow);
  const done = recent.filter((t) => t.status === "done").length;
  const failed = recent.filter(isRealFailure).length;
  const parked = recent.filter(isNeedsYou).length;
  const cost = spend?.cost_usd ?? 0;
  const tokens = spend?.tokens ?? 0;
  // "Quiet" must never hide spend: a night where an in-flight task burned
  // real tokens is not quiet, even with zero terminal events.
  return { done, failed, parked, cost, tokens,
           quiet: done + failed + parked === 0 && !(cost || tokens) };
}
