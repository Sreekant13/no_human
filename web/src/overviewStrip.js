// The always-on header strip. SCRUM-84: it used to mix now-state ("2 need
// you", "oldest <1m") with UNLABELED all-time outcome tallies ("17 failed ·
// 75 cancelled") — a second, differently-scoped "failed" number sitting next
// to the sidebar's 24h-scoped ledger count, with nothing telling them apart.
// Outcome totals already live in the Failed nav badge and the Stats page, so
// this strip now reports ONLY live state: what needs the operator right now,
// how long the oldest of those has waited, and what is actively running.
import { isNeedsYou, deriveCounts } from "./boardLanes.js";
import { timestampMs } from "./parseTimestamp.js";

export function overviewState(tasks, now = Date.now()) {
  const list = Array.isArray(tasks) ? tasks : [];
  const needsYou = list.filter(isNeedsYou);
  const counts = deriveCounts(list);
  const oldestWaitingSec = needsYou.length > 0
    ? Math.max(...needsYou.map((t) => (now - timestampMs(t.updated_at || t.created_at, 0)) / 1000))
    : null;
  return {
    needsYouCount: needsYou.length,
    allClear: needsYou.length === 0,
    oldestWaitingSec,
    running: counts.running,
    queued: counts.queued,
  };
}
