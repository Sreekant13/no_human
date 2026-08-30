// The board card's five facts: title, one status line, one meta line, one
// action, and age (age is rendered by the caller, alongside metaLine). Every
// other chip the card used to carry — repo, source, kind, spec badge,
// cancelled badge, substatus, priority — moved to the drawer's Details tab
// (SlideOver.jsx DetailsTab); this is the one place that decides what those
// five facts say.
//
// `cost` is a plain dollar number (or null/undefined), not an API field:
// TaskSummaryOut has no cost_usd — callers compute it once via
// cost.js's taskCost(task) and pass it in, so this stays a pure function of
// its two arguments and is trivial to unit test without a token-bucket fixture.
import { cardBlockerLine } from "./cardBlockerLine.js";
import { isWaiting } from "./boardLanes.js";
import { showConflictBadge, conflictRoundLabel } from "./conflictStatus.js";
import { fmtCost } from "./cost.js";

export function cardFacts(task, { cost } = {}) {
  const title = task.title_short || task.title || "";
  const costPart = Number.isFinite(cost) && cost > 0 ? fmtCost(cost) : "";
  const metaLine = [
    task.repo_name,
    task.attempt_count > 0 ? `att ${task.attempt_count}` : "",
    costPart,
  ].filter(Boolean).join(" · ");

  // These three states end the story regardless of `status` — an operator
  // cancel, an approval already given, or a human who already answered a now-
  // stale-looking blocker. None of them leaves anything for an action button.
  if (task.cancelled) {
    return { title, statusLine: "Cancelled", metaLine, action: null };
  }
  if (task.approved_at) {
    return { title, statusLine: "Approved — merge pending", metaLine, action: null };
  }
  if (task.blocker_human_stopped) {
    return { title, statusLine: "Stopped by you — parked", metaLine, action: null };
  }

  const status = task.status;
  // A bounded rebase round in flight is state, not decoration — it just no
  // longer gets its own chip; it leads the status line like a live_status would.
  const conflictLine = showConflictBadge(task) ? conflictRoundLabel(task) : null;
  let statusLine = conflictLine || task.live_status || "";
  let action = null;

  if (status === "awaiting_approval") {
    action = { label: "Review PR", kind: "review" };
  } else if (status === "escalated") {
    // cardBlockerLine reshapes the BUDGET_EXHAUSTED template so two escalated
    // cards don't clamp to the same 47 identical leading characters; any other
    // question passes through unchanged.
    statusLine = cardBlockerLine(task.blocker_question) || task.live_status || "";
    action = { label: "Advise or split", kind: "answer" };
  } else if ((status === "awaiting_input" || status === "blocked") && !isWaiting(task)) {
    // `blocked` WITH a wake condition self-resolves (isWaiting) and belongs to
    // Working, not to a human — no action, no blocker text, same as before.
    statusLine = cardBlockerLine(task.blocker_question) || task.live_status || "";
    action = { label: "Answer question", kind: "answer" };
  }

  return { title, statusLine, metaLine, action };
}
