// Lanes are organized by WHAT ACTION THE HUMAN NEEDS TO TAKE. The single
// "Needs You" lane conflated two very different asks — a finished PR to
// review/approve vs a stuck task needing a decision/clarification — so it is
// split into two loud lanes:
//   Review PR    — awaiting_approval: a PR is up, review and approve/merge
//   Needs Answer — awaiting_input / escalated / blocked-without-wake: the agent
//                  needs a human decision, answer, or clarification to proceed
//   Working      — agent is actively processing (pending through testing)
//   Waiting      — auto-resolvable (will wake on its own, no human needed)
//   Failed       — terminal
//   Done         — completed
//
// Pure so the routing is node --test'd. Board.jsx consumes LANES + routeTask.
// Order is left→right by narrative: attention-now → in-flight → outcomes.
// "Review PR" is the last POSITIVE step before Done (approve → merge → done),
// so it sits right beside Done and is coloured with the semantic review purple
// (--c-review) — NOT the blue of Working, which is what made it look like just
// another in-progress lane.
// `emptyHint` / `emptyIcon` give each lane an intentional empty state instead of
// a blank void — the WORKING and WAITING columns are often empty and used to
// read as dead space. Copy is calm and explains the *meaning* of empty here.
export const LANES = [
  { key: "answer",  label: "Needs Answer", accent: "var(--c-answer)",    statuses: ["awaiting_input", "escalated"], loud: true, needsYou: true, emptyIcon: "✓", emptyHint: "All caught up — nothing needs your input" },
  { key: "working", label: "Working",      accent: "var(--c-building)",  statuses: ["pending", "context", "planning", "implementing", "reviewing", "testing", "compound_parent"], showSubStatus: true, emptyIcon: "○", emptyHint: "No tasks in flight" },
  { key: "waiting", label: "Waiting",      accent: "var(--c-context)",   statuses: ["blocked", "paused_quota"], autoWait: true, emptyIcon: "◷", emptyHint: "Nothing parked — waiting tasks wake themselves" },
  { key: "failed",  label: "Failed",       accent: "var(--c-escalated)", statuses: ["failed"], emptyIcon: "○", emptyHint: "No failures" },
  { key: "review",  label: "Review PR",    accent: "var(--c-review)",    statuses: ["awaiting_approval"], loud: true, needsYou: true, emptyIcon: "○", emptyHint: "No PRs waiting for review" },
  { key: "done",    label: "Done",         accent: "var(--c-done)",      statuses: ["done"], emptyIcon: "○", emptyHint: "Nothing shipped yet" },
];

// "blocked" is routed dynamically: WITH a wake_condition it auto-resolves →
// Waiting; WITHOUT, a human must act → Needs Answer.
export function routeTask(task) {
  if (task.status === "blocked") {
    return task.blocker_wake_condition ? "waiting" : "answer";
  }
  for (const lane of LANES) {
    if (lane.statuses.includes(task.status)) return lane.key;
  }
  return "working";
}

const NEEDS_YOU_LANES = new Set(LANES.filter((l) => l.needsYou).map((l) => l.key));

// SINGLE source of truth for "this task needs a human" — the same routing the
// board uses. A status-only set drifted from the lanes (blocked-without-wake
// sits in Needs Answer but a status set missed it, so the header said "6 need
// you" while the lanes showed 7). Count, badge, and notifications all use this.
export function isNeedsYou(task) {
  return NEEDS_YOU_LANES.has(routeTask(task));
}
