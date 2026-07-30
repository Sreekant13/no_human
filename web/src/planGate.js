// The human plan-approval gate (GAP 1), board side.
//
// A task parked on the gate is an ordinary `awaiting_input` task carrying an
// ordinary blocker — nothing on this side needed a new status or a new lane.
// The one thing the board must not get wrong is WHICH reply means "approved":
// the approval is structural (an option carrying `{approve_plan: true}`), never
// a string match on the answer text, because free text at the gate is a
// correction that re-plans.

import { normalizeOptions } from "./blockerOptions.js";

// The 1-based index (what POST /reply's `choose` expects) of the option that
// approves the parked plan, or null when this task has no such option.
export function planApproveIndex(task) {
  const options = normalizeOptions(task?.blocker?.options);
  const i = options.findIndex((o) => o.action?.approve_plan === true);
  return i === -1 ? null : i + 1;
}
