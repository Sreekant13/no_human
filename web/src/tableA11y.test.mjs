import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import postcss from "postcss";

// Guards the task table's keyboard contract — the parts static analysis can soundly prove.
//
// The rows in TaskTable.jsx are the control itself (role="button", tabIndex=0), not a
// container with a button inside. So the row needs its own focus indicator, and the
// deliberate 0.5-opacity muting of cancelled rows has to lift for the keyboard the same
// way it lifts for the mouse. Both are easy to lose in a restyle and invisible to anyone
// testing with a mouse.
//
// SCOPE, deliberately narrow: these tests assert that the rules EXIST and that the markup
// is still a control. They do NOT try to decide whether a colour "looks visible" — three
// review rounds established that a static evaluator of CSS visibility is unsound (it
// passed `outline: none !important`, `outline: medium none`, an 8-digit-hex zero alpha,
// `calc(0px)` widths, and a ring recoloured to the row's own background, while also
// falsely accusing legitimate edits). If you find yourself re-implementing CSS semantics
// here, that is the mistake — measure it in a browser instead, where the cascade is
// already resolved. Whether the ring PAINTS (stacking, overlap, clipping) needs a real
// engine and is not claimed by any test in this repo.

const SRC = dirname(fileURLToPath(import.meta.url));
const root = postcss.parse(readFileSync(join(SRC, "styles.css"), "utf8"));

function rulesFor(cls) {
  const re = new RegExp(`\\.${cls}(?![a-zA-Z0-9_-])`);
  const out = [];
  root.walkRules((rule) => {
    if (!re.test(rule.selector)) return;
    const atRules = [];
    for (let p = rule.parent; p && p.type !== "root"; p = p.parent) {
      if (p.type === "atrule") atRules.unshift(`@${p.name} ${p.params}`.trim());
    }
    out.push({ selector: rule.selector, atRules, rule });
  });
  return out;
}

test("the task-table row is still a real, focusable control", () => {
  // Everything else here guards CSS that only matters because the row is focusable at
  // all. Delete tabIndex and the feature is gone while every style assertion stays green.
  const jsx = readFileSync(join(SRC, "TaskTable.jsx"), "utf8");
  // Slice from the CONDITIONAL spread, not the first mention of onSelect — the first hit
  // is in this file's doc comment, which would make `gated` the whole file and the
  // "these live in the onSelect branch" claim below vacuous.
  const spreadAt = jsx.indexOf("...(onSelect");
  assert.ok(spreadAt > 0, "TaskTable no longer spreads row props conditionally on onSelect");
  const gated = jsx.slice(spreadAt);
  assert.match(gated, /role:\s*"button"/, "rows lost role=button — they no longer announce as controls");
  assert.match(gated, /tabIndex:\s*0/, "rows lost tabIndex=0 — they are no longer keyboard reachable");
  assert.match(jsx, /stats-tr-clickable/, "rows no longer carry .stats-tr-clickable — the focus styles target nothing");
});

test("a focus rule exists for the clickable row, and applies unconditionally", () => {
  const all = rulesFor("stats-tr-clickable");
  assert.ok(all.length > 0, ".stats-tr-clickable has no rules at all");
  // Unconditional: a focus style reachable only inside `@media print` (or any query) is
  // not a focus style for the app. Existence and reachability are soundly checkable here.
  // Whether it is VISIBLE is not, and is deliberately not asserted — see the header.
  const focus = all.filter((r) => /:focus/.test(r.selector) && r.atRules.length === 0);
  assert.ok(
    focus.length > 0,
    `.stats-tr-clickable is focusable (tabIndex=0) but has no unconditional :focus rule.\n` +
      `  rules found: ${all.map((r) => `${r.atRules.join(">")}${r.selector}`).join(" | ")}`,
  );
});

test("a cancelled row un-mutes for the keyboard wherever it un-mutes for the mouse", () => {
  const unmute = rulesFor("stats-tr-cancelled").filter(
    (r) =>
      r.atRules.length === 0 &&
      r.rule.nodes.some((n) => n.type === "decl" && n.prop === "opacity" && n.value.trim() === "1"),
  );
  assert.ok(unmute.length > 0, "no rule restores .stats-tr-cancelled to full opacity");

  // Compare the SHAPE of the selectors, not just "is the word focus present somewhere".
  // A focus branch retargeted to `.stats-td-nope`, or given an ancestor that never
  // matches, keeps the substring while restoring nothing — so require that the focus
  // selector is the hover selector with only the pseudo-class swapped.
  const parts = unmute.flatMap((r) => r.selector.split(",").map((s) => s.trim()));
  const shape = (s) => s.replace(/:focus-within|:focus-visible|:focus\b/, "§").replace(/:hover/, "§");
  const hoverShapes = new Set(parts.filter((s) => /:hover/.test(s)).map(shape));
  const focusShapes = new Set(parts.filter((s) => /:focus/.test(s)).map(shape));
  assert.ok(hoverShapes.size > 0, `expected a :hover un-mute, got: ${parts.join(" | ")}`);
  const missing = [...hoverShapes].filter((s) => !focusShapes.has(s));
  assert.deepEqual(
    missing,
    [],
    `.stats-tr-cancelled un-mutes on :hover but not on focus for: ${missing.join(" | ")}\n` +
      `  a keyboard user reads a focused cancelled row at 0.5 opacity.\n` +
      `  selectors: ${parts.join(" | ")}`,
  );
});
