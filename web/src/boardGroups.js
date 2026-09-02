// U4: the FAILED lane is a graveyard — one stubborn task retried N ways
// buries the board under near-identical cards (live: 5 of 9 lifetime tasks
// share one title). Collapse same-title failed cards to ONE representative plus
// a count; the older ones stay reachable through it. Pure, node --test'd.

import { isRealFailure } from "./boardLanes.js";
import { timestampMs, compareDesc } from "./parseTimestamp.js";

// Which card heads the group. Newest wins — EXCEPT that an operator-cancelled task also
// ends in `failed` status, so "newest" alone let a cancel head a group and bury the one
// real failure inside "+N older", where neither the operator nor the lane's ordering could
// see it (live: a cancel created 11:26 outranked the real failure created 11:05, same title).
// A real failure always outranks a cancel; within each class, newest wins.
//
// `created_at` compares through `timestampMs` (epoch ms), not the raw string: the DB stores
// both naive-space 'YYYY-MM-DD HH:MM:SS' and iso-offset '...+00:00' timestamps in the same
// column, and ' ' < 'T' lexically, so a raw `>` always ranked a naive-space row as older than
// an iso-offset row from the same date regardless of actual age.
function outranks(candidate, current) {
  const realA = isRealFailure(candidate);
  const realB = isRealFailure(current);
  if (realA !== realB) return realA;
  return compareDesc(timestampMs(candidate.created_at), timestampMs(current.created_at)) < 0;
}

export function groupFailedByTitle(tasks) {
  const byTitle = new Map();
  for (const t of tasks) {
    const key = (t.title || "").trim() || t.id;
    const g = byTitle.get(key);
    if (!g) byTitle.set(key, { newest: t, older: [] });
    else if (outranks(t, g.newest)) {
      g.older.push(g.newest);
      g.newest = t;
    } else {
      g.older.push(t);
    }
  }
  return [...byTitle.values()].map(({ newest, older }) => ({
    task: newest,
    collapsedCount: older.length,
    olderIds: older
      .sort((a, b) => compareDesc(timestampMs(a.created_at), timestampMs(b.created_at)))
      .map((t) => t.id),
  }));
}
