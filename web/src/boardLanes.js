// Lanes are organized by WHAT ACTION THE HUMAN NEEDS TO TAKE:
//   Needs Answer — awaiting_input / escalated / blocked-without-wake: the agent
//                  needs a human decision, answer, or clarification to proceed
//   Working      — agent is in-flight OR parked-but-self-resolving (blocked with
//                  a wake condition, paused_quota). The old separate "Waiting"
//                  lane held only auto-resolving tasks and was empty most of the
//                  time — dead horizontal space. That "wakes itself, no human
//                  needed" distinction now lives on the CARD (isWaiting →
//                  "◷ waits for its own signal"), not a whole column.
//   Review PR    — awaiting_approval: a PR is up, review and approve/merge
//   Failed       — terminal
//   Done         — completed
//
// Pure so the routing is node --test'd. Board.jsx consumes LANES + routeTask.
// Order is left→right by narrative: attention-now → in-flight → outcomes.
// "Review PR" is the last POSITIVE step before Done (approve → merge → done),
// so it sits right beside Done and is coloured with the semantic review purple
// (--c-review) — NOT the blue of Working.
export const LANES = [
  { key: "answer",  label: "Needs Answer", accent: "var(--c-answer)",    statuses: ["awaiting_input", "escalated"], loud: true, needsYou: true, staleCollapse: true, emptyIcon: "✓", emptyHint: "All caught up — nothing needs your input" },
  { key: "working", label: "Working",      accent: "var(--c-building)",  statuses: ["pending", "context", "planning", "implementing", "reviewing", "testing", "compound_parent", "paused_quota"], showSubStatus: true, emptyIcon: "○", emptyHint: "No tasks in flight" },
  { key: "failed",  label: "Failed",       accent: "var(--c-escalated)", statuses: ["failed"], outcome: true, emptyIcon: "○", emptyHint: "No failures" },
  { key: "review",  label: "Review PR",    accent: "var(--c-review)",    statuses: ["awaiting_approval"], loud: true, needsYou: true, emptyIcon: "○", emptyHint: "No PRs waiting for review" },
  { key: "done",    label: "Done",         accent: "var(--c-done)",      statuses: ["done"], outcome: true, emptyIcon: "○", emptyHint: "Nothing shipped yet" },
];

// 5D: the board shows only the lanes that represent a GATE — something the human owes. Done and
// Failed are OUTCOMES: they competed for width with the three lanes that actually need attention,
// and an outcome list reads better as a sortable table than as a column of cards. They move to two
// buttons above the connection indicator, which open that table.
//
// They stay in LANES on purpose: LANES is also the ROUTING table (routeTask iterates it), so
// filtering it here would make a done task fall through to "working" — finished work would come
// back as in-flight.
export const BOARD_LANES = LANES.filter((l) => !l.outcome);
export const OUTCOME_LANES = LANES.filter((l) => l.outcome);

// "blocked" routes dynamically: WITH a wake_condition it self-resolves → Working
// (shown as parked on the card); WITHOUT, a human must act → Needs Answer.
export function routeTask(task) {
  if (task.status === "blocked") {
    return task.blocker_wake_condition ? "working" : "answer";
  }
  for (const lane of LANES) {
    if (lane.statuses.includes(task.status)) return lane.key;
  }
  return "working";
}

// A task sitting in Working that is parked on its own signal (not actively being
// processed) — so the card can say "waits for its own signal" instead of looking
// like live work. This is the distinction the old Waiting column carried.
export function isWaiting(task) {
  return (
    task.status === "paused_quota" ||
    (task.status === "blocked" && !!task.blocker_wake_condition)
  );
}

const NEEDS_YOU_LANES = new Set(LANES.filter((l) => l.needsYou).map((l) => l.key));

// SINGLE source of truth for "this task needs a human" — the same routing the
// board uses. A status-only set drifted from the lanes (blocked-without-wake
// sits in Needs Answer but a status set missed it, so the header said "6 need
// you" while the lanes showed 7). Count, badge, and notifications all use this.
export function isNeedsYou(task) {
  // B2 #19: an APPROVED PR still sits in awaiting_approval until the merge
  // lands — it is not waiting on you any more, so it must stop shouting in
  // "N need you". It STAYS in the Review lane (routing is untouched: LANES is
  // also the routing table) but renders as "approved — merge pending".
  if (task?.approved_at) return false;
  // A human who explicitly stopped a parked task already gave their answer
  // (blocker.human_stopped, flattened as blocker_human_stopped). Stop it
  // shouting in "N need you", but keep its lane — routing untouched, exactly
  // like the approved_at guard above.
  if (task?.blocker_human_stopped) return false;
  return NEEDS_YOU_LANES.has(routeTask(task));
}

// A cancelled task ends in FAILED status but is NOT a capability failure. The board's
// overview strip counted every `failed` row, so a board with 1 real failure and 10
// operator-cancelled tasks shouted "11 failed" — a permanent red alarm competing with
// the actual gates. Stats already excluded cancels; this is now the one definition both
// surfaces share.
export function isRealFailure(task) {
  return Boolean(task) && task.status === "failed" && !task.cancelled;
}

// SCRUM-15: the scheduler's in-flight set is the ONLY thing that means "the
// agent is actually working on this right now" — an active-status task the
// scheduler hasn't picked up yet is queued, not running, no matter how it looks.
export function isRunning(task) {
  return task?.claimed === true;
}

// Queued = routed to Working, but not actually claimed, and not parked on its
// own signal (that's `waiting`, a distinct, non-active state with its own tag).
export function isQueued(task) {
  return routeTask(task) === "working" && !isRunning(task) && !isWaiting(task);
}

// Card treatment decision, extracted so JSX just consumes it instead of
// re-deriving "is this active" from raw status (the bug this ticket fixes).
export function cardActivity(task) {
  if (isRunning(task)) return { mode: "running", showPulse: true, showQueuedChip: false, mutedProgress: false };
  if (isQueued(task)) return { mode: "queued", showPulse: false, showQueuedChip: true, mutedProgress: true };
  if (isWaiting(task)) return { mode: "waiting", showPulse: false, showQueuedChip: false, mutedProgress: true };
  return { mode: "idle", showPulse: false, showQueuedChip: false, mutedProgress: false };
}

// SINGLE source of truth for the top strip / lane headers / sidebar counts —
// they must all read from here so they cannot disagree (the bug this ticket
// fixes: three surfaces derived "working" from three different vocabularies).
export function deriveCounts(tasks) {
  const t = Array.isArray(tasks) ? tasks : [];
  const running = t.filter(isRunning).length;
  const queued = t.filter(isQueued).length;
  const waiting = t.filter(isWaiting).length;
  return {
    running,
    queued,
    waiting,
    working: running + queued + waiting, // == Working-lane header, by construction
    needsYou: t.filter(isNeedsYou).length,
  };
}
