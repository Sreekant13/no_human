// SCRUM-19: the Needs-Answer lane led with day-old escalations while tonight's
// real ones sat below them. Presentation-only fix: sort by escalation recency
// (newest first) and split off anything older than 24h into a collapsed,
// muted group — nothing is dismissed, filtered, or dropped (fresh.length +
// stale.length === items.length, always).

export const STALE_ANSWER_MS = 24 * 3600 * 1000;

// Prefers a dedicated `escalated_at` (when the schema tracks one); otherwise
// falls back to the same recency chain the card itself already displays.
export function escalationTs(task) {
  return (task && (task.escalated_at || task.last_activity || task.updated_at || task.created_at)) || "";
}

/**
 * Split `items` into `{ fresh, stale }`, each newest-escalation-first.
 * Never mutates `items`; never drops a row — fresh.length + stale.length === items.length.
 *
 * @param items      array of tasks
 * @param nowMs      current time in ms (caller-supplied so this stays pure/testable)
 * @param thresholdMs age past which a card is "stale" (default 24h)
 * @param tsOf       timestamp accessor; defaults to escalationTs
 */
export function partitionAnswerLane(items, nowMs, thresholdMs = STALE_ANSWER_MS, tsOf = escalationTs) {
  if (!Array.isArray(items) || items.length === 0) {
    return { fresh: [], stale: [] };
  }
  // Copy-then-sort: never mutate the caller's array. Descending by ts, id
  // tiebreak for determinism when timestamps collide (or are both missing).
  const sorted = [...items].sort((a, b) => {
    const ta = tsOf(a) || "";
    const tb = tsOf(b) || "";
    if (ta !== tb) return ta < tb ? 1 : -1;
    const ida = String(a?.id ?? "");
    const idb = String(b?.id ?? "");
    return ida < idb ? -1 : ida > idb ? 1 : 0;
  });

  const fresh = [];
  const stale = [];
  for (const item of sorted) {
    const ts = tsOf(item);
    const ageMs = ts ? nowMs - new Date(ts).getTime() : Infinity;
    // Missing/unparseable timestamp → treated as infinitely old: buried at the
    // bottom of stale, never throws (new Date(NaN) - anything is NaN, and
    // NaN > thresholdMs is false, so an explicit Infinity fallback is required).
    const isStale = Number.isNaN(ageMs) ? true : ageMs > thresholdMs;
    (isStale ? stale : fresh).push(item);
  }
  return { fresh, stale };
}
