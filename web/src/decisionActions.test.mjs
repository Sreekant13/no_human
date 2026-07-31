import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { decisionFor } from "./blockerDecision.js";

// The escalated drawer offers a parked task three real choices. The third —
// "Stop this task" — was styled `color: var(--text-dim); border-color:
// transparent; padding: 9px 10px`, i.e. dim grey, borderless and physically
// smaller than the bordered "Reply in your own words" beside it. Measured on
// the live drawer it rendered at 2.48:1 in dark, which is not "quiet", it is
// invisible: it reads as disabled decoration, so a user scanning three choices
// sees two.
//
// It is now a quiet-DANGER button — bordered like its siblings, danger-tinted,
// but not a filled red alarm, because stopping a task is a legitimate decision
// and not a warning. These assertions pin the affordance, not the hue: the
// contrast arithmetic lives in contrast.test.mjs.

const SRC = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(join(SRC, "styles.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

const ruleFor = (selector) => {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const m = css.match(new RegExp(`(?:^|[\\s,{}>])${esc}\\s*\\{([^}]*)\\}`));
  assert.ok(m, `no rule found for ${selector} — has it been renamed?`);
  return m[1];
};

const declaration = (block, prop) =>
  block.match(new RegExp(`(?:^|[\\s;])${prop}\\s*:\\s*([^;]+)`))?.[1].trim() ?? null;

test("the panel still offers three distinct actions, with stop last", () => {
  const task = {
    status: "escalated",
    blocker: {
      category: "BUDGET_EXHAUSTED",
      options: [{ id: 1, label: "Spend more" }],
      evidence: "tokens 15,324,491/12,000,000",
    },
  };
  const ids = decisionFor(task).actions.map((a) => a.id);
  assert.deepEqual(ids, ["reply", "cancel"], "an options-bearing blocker offers reply + stop");
  assert.equal(decisionFor(task).actions.at(-1).variant, "quiet",
    "stop is still the quiet variant — this test pins its STYLING, not its rank");
});

test('"Stop this task" is a real bordered button, not dim text', () => {
  const base = ruleFor(".decision-btn");
  // The repo hazard that makes a border silently paint nothing: a border-color
  // with no border-style. The shared base must declare the style.
  assert.match(base, /border\s*:\s*\d+\S*\s+solid/,
    ".decision-btn must set a border STYLE — a border-color alone paints nothing");

  const quiet = ruleFor(".decision-btn-quiet");
  const border = declaration(quiet, "border-color");
  assert.ok(border, ".decision-btn-quiet must set its own border-color");
  assert.doesNotMatch(border, /^(transparent|none)$/,
    `.decision-btn-quiet border-color is "${border}" — that is the invisible state this fixes`);

  const color = declaration(quiet, "color");
  assert.match(color, /var\(\s*--(red|danger)\s*\)/,
    `.decision-btn-quiet colour is "${color}" — it must be danger-tinted, not the dim meta grey`);
  assert.ok(!/--text-dim|--fg-dim/.test(color),
    ".decision-btn-quiet must not reuse the dim meta token");
});

test('"Stop this task" is the same size as the buttons beside it', () => {
  // It carried `padding: 9px 10px` against the shared `9px 16px`, so it was
  // visibly smaller than its siblings — the other half of why it read as a
  // link. It must not re-acquire a narrower padding override.
  const basePad = declaration(ruleFor(".decision-btn"), "padding");
  assert.ok(basePad, ".decision-btn must define the shared padding");
  const quietPad = declaration(ruleFor(".decision-btn-quiet"), "padding");
  assert.ok(quietPad === null || quietPad === basePad,
    `.decision-btn-quiet overrides padding to "${quietPad}" instead of the shared "${basePad}"`);
});

test("stop stays visually separated from the safe actions", () => {
  // Deliberate, and worth keeping: the destructive choice is pushed to the far
  // right so it is never the button you hit by momentum.
  assert.match(ruleFor(".decision-btn-quiet"), /margin-left\s*:\s*auto/,
    ".decision-btn-quiet must stay pushed away from the safe actions");
});
