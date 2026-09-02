// 5B: a lane shows only its top-N most-recent items; the rest hide behind an
// expand arrow. Recency matches the card's OWN displayed timestamp
// (Board.jsx: last_activity || updated_at || created_at), NOT bare updated_at.
// Pure so it's node --test'd; the count badge keeps showing the true total.

import { timestampMs, compareDesc } from "./parseTimestamp.js";

const defaultTs = (x) =>
  (x && (x.last_activity || x.updated_at || x.created_at)) || "";

/**
 * Sort `items` by recency (newest first, stable for ties) and split into the
 * visible top-N and the hidden remainder.
 *
 * @param items  array of tasks (or failed-lane groups via a custom tsOf)
 * @param n      how many to show before collapsing
 * @param tsOf   timestamp accessor; defaults to the card's recency fallback
 * @returns {{visible: any[], hiddenCount: number}}
 */
export function topByRecency(items, n, tsOf = defaultTs) {
  if (!Array.isArray(items) || items.length === 0) {
    return { visible: [], hiddenCount: 0 };
  }
  // Stable desc sort: equal timestamps keep input order (JS sort is stable, and
  // the comparator returns 0 on a tie). Copy first — never mutate the caller's.
  // Compares through timestampMs (epoch ms), not the raw string: `tsOf` values
  // come straight off `*_at` fields, and the DB stores both naive-space
  // 'YYYY-MM-DD HH:MM:SS' and iso-offset '...+00:00' timestamps in the same
  // column — ' ' < 'T' lexically, so a raw `<`/`>` always ranked a naive-space
  // row as older than an iso-offset row from the same date regardless of age.
  const sorted = [...items].sort((a, b) =>
    compareDesc(timestampMs(tsOf(a)), timestampMs(tsOf(b))),
  );
  const take = Math.max(0, n | 0);
  return {
    visible: sorted.slice(0, take),
    hiddenCount: Math.max(0, sorted.length - take),
  };
}

/**
 * Like {@link topByRecency}, but items matching `isPriority` take the visible slots
 * FIRST, each group still ordered by recency.
 *
 * The Failed lane needs this: a cancelled task ends in `failed` status, so ten cancels
 * push the one REAL failure — the only row that wants attention — behind "Show N more"
 * purely because a cancel happened to be more recent.
 *
 * With no predicate this is exactly {@link topByRecency}.
 */
export function topPrioritised(items, n, tsOf = defaultTs, isPriority = null) {
  if (!Array.isArray(items) || items.length === 0) {
    return { visible: [], hiddenCount: 0 };
  }
  if (!isPriority) return topByRecency(items, n, tsOf);

  const priority = items.filter((x) => isPriority(x));
  const rest = items.filter((x) => !isPriority(x));
  const top = topByRecency(priority, n, tsOf);
  const fill = topByRecency(rest, Math.max(0, n - top.visible.length), tsOf);
  return {
    visible: [...top.visible, ...fill.visible],
    hiddenCount: top.hiddenCount + fill.hiddenCount,
  };
}
