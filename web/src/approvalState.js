// The single JS-side predicate for "does this task carry a LIVE, unresolved
// approval" — the twin of `core/lanes.py::approval_pending`. Every board/
// drawer surface that used to check a bare `task.approved_at` derives the
// "approved - merge pending" chip, and the needs-you count suppression, from
// this module instead, so none of them can ever again contradict the lane a
// task is actually sitting in.
//
// A bare `approved_at` is not enough: `core/db.py::_write_status` stamps
// `context.approval_superseded_at` (write-once) the moment a row leaves
// awaiting_approval for anything other than done — an escalation, a
// conflict-round send-back, or a fresh attempt — so an approval recorded
// before one of those no longer reads as pending once the row has moved on.
// `approved_at` itself is never cleared (it stays the audit trail).
//
// Both helpers below accept two payload shapes: the flat TaskSummaryOut
// fields the board/API actually send (`task.approved_at`,
// `task.approval_superseded_at`) and a raw `task.context.*` shape, the same
// dual-shape tolerance `slideOverSummary.js`'s `taskApprovedAt` already had
// (older fixtures / direct context objects construct the latter).

export function approvedAtOf(task) {
  return task?.approved_at || task?.context?.approved_at || null;
}

export function supersededAtOf(task) {
  return task?.approval_superseded_at || task?.context?.approval_superseded_at || null;
}

// True only when the approval is BOTH recorded and unresolved AND the task
// is still sitting in the gate it was approved out of — belt and suspenders
// against the exact contradiction this module exists to close. `done` is
// deliberately excluded by the status check: a completed merge is the
// approval's success, not something this stricter, status-gated predicate
// needs to speak for (contrast `taskApprovedAt` in slideOverSummary.js,
// which intentionally keeps reporting a live approval on a `done` row so the
// merge-complete narrative still reads correctly there).
export function approvalLive(task) {
  if (!approvedAtOf(task)) return false;
  if (supersededAtOf(task)) return false;
  return task?.status === "awaiting_approval";
}
