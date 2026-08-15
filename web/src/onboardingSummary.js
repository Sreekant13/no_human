// Both repo rows of the launch summary, derived from ONE input — the server's
// readiness payload (the persisted profile store). Never from the wizard's
// tick state: that is this mount's checkboxes, and it is empty after a
// reload or a re-run while the profiles survive server-side. That divergence
// WAS the bug this closes: "Repos 0" beside "0 of 1" for the same single
// registered repo — one row read local ticks, the other read the server.
//
// `readiness` is whatever `fetchReadiness()` last produced: `null` while the
// request is in flight, `{ error: true }` if it rejected, or the server's
// `{ total, usable, ... }` object.
export function summaryRepoCounts(readiness) {
  if (readiness === null || readiness === undefined) {
    return { repos: "…", proven: "…", total: null, usable: null };
  }
  if (readiness.error) {
    return { repos: "—", proven: "—", total: null, usable: null };
  }
  const total = Number(readiness.total) || 0;
  const usable = Number(readiness.usable) || 0;
  return { repos: String(total), proven: `${usable} of ${total}`, total, usable };
}
