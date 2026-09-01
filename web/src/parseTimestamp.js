// The server writes timestamps in two live formats: SQLite `datetime('now')`
// ("2026-09-01 12:00:00", UTC, NO zone marker) and Python `_now()` ISO
// ("2026-09-01T12:00:00+00:00"). `new Date(s)` reads the FIRST as LOCAL time
// (the ES2015+ date-time forms without an offset are defined as local), so in
// UTC+3 every "time ago" and the Stats tasks/day span read 3h stale — a
// 40-minute-old task showed "3h ago". Same two-format fact is documented
// server-side at src/no_human/core/db.py:3555.
//
// This is the ONE place that decides how a timestamp string becomes a Date:
// every call site in web/src routes through here instead of calling
// `new Date(...)`/`Date.parse(...)` on a raw API value itself.
const NAIVE = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?$/;

const warned = new Set();

function warnOnce(value) {
  if (warned.has(value)) return;
  warned.add(value);
  console.warn("[parseTimestamp] unrecognized timestamp, ignoring:", value);
}

/**
 * Parse a server-supplied timestamp into a `Date`, treating a naive
 * `YYYY-MM-DD HH:MM:SS[.ffffff]` string (SQLite's `datetime('now')` shape) as
 * UTC. A string that already carries an offset or `Z` (ISO with zone,
 * RFC-2822, date-only) is handed to `new Date` unchanged. Returns `null` for
 * a missing value (no warning — a missing timestamp is normal) or a value
 * that fails to parse (warns once per distinct raw value, then degrades
 * gracefully rather than throwing).
 */
export function parseTimestamp(value) {
  if (value == null) return null;
  if (value instanceof Date) {
    return Number.isNaN(value.getTime()) ? null : value;
  }
  if (typeof value === "number") {
    return Number.isFinite(value) ? new Date(value) : null;
  }
  if (typeof value !== "string") {
    warnOnce(value);
    return null;
  }
  const s = value.trim();
  if (s === "") return null;

  const d = NAIVE.test(s) ? new Date(`${s.replace(" ", "T")}Z`) : new Date(s);
  if (Number.isNaN(d.getTime())) {
    warnOnce(value);
    return null;
  }
  return d;
}

/** Same parse, as epoch milliseconds. `NaN` (not `null`) on failure, so the
 * existing `Number.isNaN(...)` guards at every call site stay byte-identical. */
export function timestampMs(value, fallback = NaN) {
  const d = parseTimestamp(value);
  return d ? d.getTime() : fallback;
}
