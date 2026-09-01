// P3: the create-time feasibility toast.
//
// Feature #1's pre-flight hint (core/feasibility.py) has been stashed on
// task.context since the create handler landed, and TaskSummaryOut now
// carries it on the CREATE RESPONSE itself (models.py `feasibility_hint`).
// But the only place that ever rendered it was SlideOver's FeasibilityCard,
// gated on `task.status === "pending"` — and dispatch takes ~9s, so by the
// time an operator opens the drawer the task has usually already moved past
// pending and the card never shows. This module is the seam that reads the
// hint straight off the create response, independent of whatever the task's
// live status has become by the time anyone looks — the card variant in
// SlideOver is untouched and still gates on "pending" for a task reopened
// later.

/**
 * Build the toast payload for a just-created task's feasibility hint, or
 * `null` when the create response carried none — the fail-open mirror of
 * `estimate_feasibility` itself: nothing worth flagging renders nothing, not
 * an empty toast.
 */
export function feasibilityCreateToast(created) {
  const hint = created && created.feasibility_hint;
  if (!hint || !hint.message) return null;
  return {
    id: `feasibility-create-${created.id}`,
    message: hint.message,
    tier: hint.tier || null,
  };
}
