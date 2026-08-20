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

// 2026-08-20 evidence: `/api/queue/health` reported "not stuck, 0 busy, 7
// queued, ETA 210 min" while the pool sat behind a quota wall — every field
// individually true, the picture false. `paused_until` is the scheduler's
// own cooldown clock (never re-derived here); this only formats it.
export function formatPausedUntil(iso) {
  if (!iso) return "unknown time";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "unknown time";
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

// input: the flat {workers_busy, max_workers, queue_depth, est_drain_seconds,
// error, paused, paused_until} object (the raw /api/queue/health payload —
// field names match 1:1, see core/health.py QueueHealth.as_dict). error is
// the most recent poll's failure (if any) — that always wins over any cached
// counts, per the AC's explicit unreachable state. A quota pause wins over
// the normal busy/queued/ETA readout: reporting "0/4 workers busy · 7
// queued · ~3.5 h to drain" with no word of a wall is the defect this exists
// to close.
export function drainChip({
  workers_busy = 0,
  max_workers = 0,
  queue_depth = 0,
  est_drain_seconds = null,
  error = null,
  paused = false,
  paused_until = null,
} = {}) {
  if (error) return { text: "server unreachable", tone: "error" };
  if (paused) return { text: `Paused — quota resets ${formatPausedUntil(paused_until)}`, tone: "warn" };

  const parts = [`${workers_busy}/${max_workers} workers busy`, `${queue_depth} queued`];
  if (queue_depth > 0) parts.push(formatDrainEta(est_drain_seconds));

  const tone = max_workers > 0 && workers_busy >= max_workers && queue_depth > 0 ? "warn" : "ok";
  return { text: parts.join(" · "), tone };
}
