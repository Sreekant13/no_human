// SCRUM-71 (2/3 of SCRUM-67's split): pure formatting for the board header's
// live drain readout. Kept out of the JSX so the handful of states (idle /
// partial / full / no-estimate / unreachable) are unit-testable with
// `node --test` — the same pattern as integrationChip.js's statusChip().
//
// Signature is the flat AC-mandated shape: {workers_busy, max_workers,
// queue_depth, est_drain_seconds|null, error?}. The real backend payload
// (src/no_human/core/health.py: eta_minutes/open_tasks) uses different field
// names — mapping that payload onto this shape, and wiring the chip into the
// header, is SCRUM-71 3/3's job, not this module's.
//
// Never fabricates a number: an unknown ETA renders as "no estimate", not a
// guess. tone is a closed string token ("ok"/"warn"/"error"), consumed
// elsewhere as `tone-${tone}` classes — no CSS vars are introduced here.

export function formatDrainEta(seconds) {
  if (seconds == null) return "no estimate";
  if (seconds < 3600) return `~${Math.max(1, Math.round(seconds / 60))} min to drain`;
  return `~${(seconds / 3600).toFixed(1)} h to drain`;
}

// input: the flat {workers_busy, max_workers, queue_depth, est_drain_seconds,
// error} object. error is the most recent poll's failure (if any) — that
// always wins over any cached counts, per the AC's explicit unreachable state.
export function drainChip({
  workers_busy = 0,
  max_workers = 0,
  queue_depth = 0,
  est_drain_seconds = null,
  error = null,
} = {}) {
  if (error) return { text: "server unreachable", tone: "error" };

  const parts = [`${workers_busy}/${max_workers} workers busy`, `${queue_depth} queued`];
  if (queue_depth > 0) parts.push(formatDrainEta(est_drain_seconds));

  const tone = max_workers > 0 && workers_busy >= max_workers && queue_depth > 0 ? "warn" : "ok";
  return { text: parts.join(" · "), tone };
}
