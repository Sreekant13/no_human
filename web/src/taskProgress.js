// Pipeline-stage progress for a task — a rough "how far along" indicator.
// It is STAGE-based, not a precise completion estimate: a task at "implementing"
// may be on attempt 1 or attempt 3. Terminal/parked states (blocked, failed,
// escalated, awaiting_input, paused_quota) return null — there is no meaningful
// forward %, so the card shows the status instead of a bar.
const PIPELINE_PCT = {
  pending: 5,
  context: 15,
  planning: 30,
  implementing: 55,
  reviewing: 75,
  testing: 88,
  awaiting_approval: 96,
  done: 100,
};

// The percent to show, or null when a bar would mislead (parked/terminal).
export function taskProgress(status) {
  const pct = PIPELINE_PCT[status];
  return pct == null ? null : pct;
}
