// The one line a board card shows for a parked task's blocker.
//
// Why this exists: BUDGET_EXHAUSTED is the most common park, and the backend
// writes it from a fixed template (see _park_budget in the bounded loop):
//
//   "This task has exhausted its lifetime budget (tokens 15,324,491/12,000,000).
//    Spend more, or stop here?"
//
// Every one of those is byte-identical for the first 47 characters, and the
// card clamps the text to two lines. So a lane holding several of them renders
// several indistinguishable grey paragraphs: nothing on the card says which
// task burned far past its cap and which barely tipped over it, short of
// opening each drawer in turn.
//
// So the CARD leads with the number that differs. This is a rendering choice,
// not a rewrite of the agent's words: the agent's own trailing ask is carried
// through verbatim, the drawer still shows the full sentence, and anything that
// does not match the template is passed through untouched.

import { fmtTokens } from "./cost.js";

// The other half of the same job, for the other outcome. With
// `budget.exhaustion_terminal` on (the default) the backend no longer ASKS
// "spend more, or stop here?" — the answer was standing policy, so the task
// ends as `failed` carrying a blocker with NO question at all. The row that
// results would otherwise read "failed" and nothing else, which is strictly
// less than the escalation it replaced. So the one line is written here.
//
// Deliberately plain English, not the blocker's `root_cause_hypothesis`
// ("lifetime budget exhausted: cost-weighted tokens 4,102,912/4,000,000") —
// the drawer still shows that, with the full spend breakdown and the exact
// commands under "Was waiting for".
export const BUDGET_FAILED_LINE =
  "Ran out of its token budget — the ticket was probably too big. " +
  "Refile it smaller, or raise the budget explicitly.";

/**
 * @param {object|null|undefined} task a task summary (needs `status`,
 *   `blocker_category`, `cancelled`)
 * @returns {string|null} the plain-English reason a FAILED row should show, or
 *   null when there is nothing to add beyond the status itself.
 */
export function failedReasonLine(task) {
  if (!task || task.status !== "failed" || task.cancelled) return null;
  return task.blocker_category === "BUDGET_EXHAUSTED" ? BUDGET_FAILED_LINE : null;
}

// The backend template (orchestrator._park_budget), kept deliberately tight: a
// loose regex that half-matched some future blocker would silently mangle an
// agent's real question.
//
// The UNIT is captured rather than enumerated, and echoed back verbatim. That is
// not laziness — the backend renamed its token unit to "cost-weighted tokens"
// while this branch was open, and an enumerated `(tokens|attempts)` stopped
// matching and silently passed the paragraph straight through, which looks
// exactly like the bug being fixed. A board mid-migration carries both
// spellings at once — rows written before and after the rename — so both must
// keep working, and the next rename must not need a code change here.
// cardBlockerLine.test.mjs reads the template out of orchestrator.py and fails
// if this stops matching it.
const BUDGET = /^This task has exhausted its lifetime budget \(([a-z][a-z-]*(?: [a-z][a-z-]*)*) ([\d,]+)\s*\/\s*([\d,]+)\)\.\s*(.+)$/;

// Only this unit is a token count worth compacting; anything else is echoed
// with its numbers intact (attempts are single digits and must stay exact).
const COMPACTABLE = /(^|\s)tokens$/;

const toNumber = (s) => {
  const n = Number(String(s).replace(/,/g, ""));
  return Number.isFinite(n) ? n : null;
};

/**
 * @param {string|null|undefined} question the blocker's raw question
 * @returns {string|null} what the card should print, or null when there is
 *   nothing to print. Non-budget questions come back unchanged.
 */
export function cardBlockerLine(question) {
  if (question == null) return null;
  const text = String(question).trim();
  if (!text) return null;

  const m = BUDGET.exec(text);
  if (!m) return text;

  const [, unit, usedRaw, capRaw, ask] = m;
  const used = toNumber(usedRaw);
  const cap = toNumber(capRaw);
  // A template that matched but carries junk numbers is not worth guessing at.
  if (used === null || cap === null) return text;

  // Tokens are seven and eight digit numbers — "15,324,491 of 12,000,000" is
  // the same wall of digits the operator was already failing to read. fmtTokens
  // is the board's existing compaction and keeps two decimals, which is what
  // separates 12.44M from 12.26M. Attempts are single digits; leave them alone.
  // A unit we do not compact keeps the backend's own formatting, separators and
  // all — reprinting the parsed Number turned "1,500,000" into "1500000".
  const shown = COMPACTABLE.test(unit)
    ? `${fmtTokens(used)} of ${fmtTokens(cap)}`
    : `${usedRaw} of ${capRaw}`;

  return `Budget: ${shown} ${unit}. ${ask}`;
}
