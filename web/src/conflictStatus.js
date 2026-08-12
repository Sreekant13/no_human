// SCRUM-42: while a bounded rebase round (SCRUM-41) is in flight, the Working
// card's sub-status badge must say so instead of just re-showing the raw
// pipeline status. Driven entirely by the `pr_conflict_rounds` context field
// SCRUM-41 attaches to the task (surfaced on TaskSummaryOut) — never by
// matching text in send_back_feedback entries.
//
// Returns null when no conflict cycle is active (rounds falsy — covers both
// "never conflicted" and the watcher's post-MERGEABLE reset to 0), so callers
// can cleanly fall back to the plain status badge.
// Review 2026-07-25: no invented denominator — the configured
// max_pr_conflict_rounds isn't on the payload, and a hardcoded "/3" renders
// the impossible "round 4/3" when the operator configures 5. If the backend
// ever surfaces the bound (task.max_pr_conflict_rounds), show it.
// 2026-08-12: wake.py legitimately stores rounds = max+1 on the escalating
// tick (it increments before checking the cap) and that value can survive a
// human resume back into an active status — so the numerator is clamped to
// the cap for display only; the stored/escalation value is untouched.
export function conflictRoundLabel(task) {
  const rounds = task?.pr_conflict_rounds;
  if (!rounds) return null;
  const max = task?.max_pr_conflict_rounds;
  if (!max) return `resolving merge conflict — round ${rounds}`;
  const shown = Math.min(rounds, max);
  return `resolving merge conflict — round ${shown}/${max}`;
}

// The badge shows only while the task is actually back IN the pipeline for the
// rebase. Once the push lands and the task returns to awaiting_approval,
// `pr_conflict_rounds` stays >0 until the watcher's next tick confirms
// MERGEABLE and resets it — during that window the card must read normally
// (operator AC: no sticky badge in Review PR). Escalated/terminal states
// render through their own lanes untouched.
const BADGE_STATUSES = new Set([
  "pending", "context", "planning", "implementing", "reviewing", "testing",
]);

export function showConflictBadge(task) {
  return BADGE_STATUSES.has(task?.status) && !!conflictRoundLabel(task);
}
