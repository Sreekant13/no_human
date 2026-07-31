import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { cardBlockerLine } from "./cardBlockerLine.js";

// Four instances of the backend's BUDGET_EXHAUSTED template as it is actually
// emitted — same cap, different overspend. This is the shape the card has to
// tell apart; the numbers are the only thing that differs.
const LIVE = [
  "This task has exhausted its lifetime budget (tokens 15,324,491/12,000,000). Spend more, or stop here?",
  "This task has exhausted its lifetime budget (tokens 12,441,414/12,000,000). Spend more, or stop here?",
  "This task has exhausted its lifetime budget (tokens 12,257,751/12,000,000). Spend more, or stop here?",
  "This task has exhausted its lifetime budget (tokens 12,367,237/12,000,000). Spend more, or stop here?",
];

test("the distinguishing number moves to the front", () => {
  assert.equal(
    cardBlockerLine(LIVE[0]),
    "Budget: 15.32M of 12.00M tokens. Spend more, or stop here?",
  );
});

test("two budget blockers no longer open with the same 47 characters", () => {
  // The defect, stated as a measurement: on the raw text every pair shared a
  // long identical prefix, and the card clamps to two lines.
  const prefix = (a, b) => {
    let i = 0;
    while (i < a.length && i < b.length && a[i] === b[i]) i += 1;
    return i;
  };
  const rawWorst = Math.min(...LIVE.slice(1).map((q) => prefix(LIVE[0], q)));
  assert.ok(rawWorst >= 45, `sanity: the raw template shares ${rawWorst} leading chars`);

  const rendered = LIVE.map(cardBlockerLine);
  assert.equal(new Set(rendered).size, LIVE.length, "every card must render differently");
  for (let i = 1; i < rendered.length; i += 1) {
    const shared = prefix(rendered[0], rendered[i]);
    assert.ok(shared <= 10,
      `"${rendered[0]}" and "${rendered[i]}" still share ${shared} leading characters`);
  }
});

test("the attempts variant of the same template is handled", () => {
  assert.equal(
    cardBlockerLine("This task has exhausted its lifetime budget (attempts 6/6). Spend more, or stop here?"),
    "Budget: 6 of 6 attempts. Spend more, or stop here?",
  );
  assert.equal(
    cardBlockerLine("This task has exhausted its lifetime budget (attempts 10/10). Spend more, or stop here?"),
    "Budget: 10 of 10 attempts. Spend more, or stop here?",
  );
});

test("the agent's own trailing ask is carried through, never invented", () => {
  assert.match(
    cardBlockerLine("This task has exhausted its lifetime budget (tokens 8,003,842/8,000,000). Raise it, or split the task?"),
    /Raise it, or split the task\?$/,
  );
});

test("a real question from an agent is passed through untouched", () => {
  // These are the agent's words about THIS task; they are already distinct and
  // must not be reshaped.
  const real = "The PR was closed without merging. Abandon the task, or rework and reopen?";
  assert.equal(cardBlockerLine(real), real);
  const long = "Given three targeted attempts to reproduce the exact 'open SELECT cursor "
    + "blocks commit' mechanism all failed, should the next attempt instrument the REAL "
    + "failing test?";
  assert.equal(cardBlockerLine(long), long);
});

test("anything that only half-matches the template is left alone", () => {
  for (const q of [
    "This task has exhausted its lifetime budget (tokens 15,324,491/12,000,000).", // no ask
    "This task has exhausted its lifetime budget (seconds 10/5). Spend more?",      // unknown unit
    "This task has exhausted its lifetime budget. Spend more, or stop here?",       // no numbers
    "Budget: this task has exhausted its lifetime budget (tokens 1/2). Stop?",      // not at the start
  ]) {
    assert.equal(cardBlockerLine(q), q, q);
  }
});

test("missing or empty input yields nothing to render", () => {
  assert.equal(cardBlockerLine(null), null);
  assert.equal(cardBlockerLine(undefined), null);
  assert.equal(cardBlockerLine(""), null);
  assert.equal(cardBlockerLine("   "), null);
});

// There is no React renderer in this harness (see settingsOverlay.test.mjs), so
// the WIRING is pinned by reading the source, the way cardPrLink.test.mjs does.
const boardJsx = readFileSync(fileURLToPath(new URL("./Board.jsx", import.meta.url)), "utf8");

test("the board card renders the blocker through cardBlockerLine", () => {
  assert.match(boardJsx, /import \{ cardBlockerLine \} from "\.\/cardBlockerLine\.js"/);
  assert.match(
    boardJsx,
    /className="card-blocker-q"[\s\S]{0,600}<span>\{cardBlockerLine\(task\.blocker_question\)\}<\/span>/,
    "the clamped card line must go through cardBlockerLine, not print the raw question",
  );
});

test("the untouched sentence is still reachable from the card", () => {
  // Reshaping the line is only acceptable because nothing is lost: the full
  // agent text stays on the element's title, and the drawer is unchanged.
  assert.match(boardJsx, /className="card-blocker-q" title=\{task\.blocker_question\}/);
});
