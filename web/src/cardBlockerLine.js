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

// The backend template, kept deliberately tight: a loose regex that half-matched
// some future blocker would silently mangle an agent's real question.
const BUDGET = /^This task has exhausted its lifetime budget \((tokens|attempts) ([\d,]+)\s*\/\s*([\d,]+)\)\.\s*(.+)$/;

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
  const shown = unit === "tokens"
    ? `${fmtTokens(used)} of ${fmtTokens(cap)}`
    : `${used} of ${cap}`;

  return `Budget: ${shown} ${unit}. ${ask}`;
}
