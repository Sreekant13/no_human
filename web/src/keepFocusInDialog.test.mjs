// The module had no unit test at all, and its only branch was unpinned: an
// independent review neutered it to `e.preventDefault()` on every mousedown —
// which breaks caret placement, button focus and text selection — and the e2e
// suite stayed green, because the spec used `.fill()` instead of clicking.
//
// These are pure-function tests over a minimal fake event, so they pin the
// DECISION. The e2e specs pin that the decision is wired to a real dialog.
import test from "node:test";
import assert from "node:assert/strict";
import { keepFocusInDialog } from "./keepFocusInDialog.js";

// 🔴 The fakes must DISCRIMINATE, or these tests certify nothing. An earlier
// version used `matches: (sel) => sel.includes("textarea")`, which answers YES to
// any selector string containing the word — so narrowing TEXT_ENTRY from
// 'input, textarea, [contenteditable]' down to 'textarea' left every test green
// while every <input> caret in the product lost its protection. A review proved
// it with that exact mutant. These parse the selector list into tokens instead.
const tokens = (sel) => sel.split(",").map((t) => t.trim());
const el = (tag, attrs = {}) => ({
  tagName: tag.toUpperCase(),
  matches: (sel) => tokens(sel).some((t) => t === tag || t.startsWith(`${tag}[`)),
  querySelector: () => attrs.wraps || null,
  // Model `HTMLLabelElement.control` — the DOM's own answer to "what does this
  // label label?". It is null when `htmlFor` points at an id that does not
  // exist, which `querySelector` alone cannot tell you.
  ...(tag === "label" ? { control: attrs.control ?? attrs.wraps ?? null } : {}),
  ...attrs,
});

/** A stand-in for the element the operator is typing into. */
const textarea = el("textarea");
const textInput = el("input");

/**
 * @param target      what the mousedown landed on
 * @param activeEl    what currently has focus (globally)
 * @param contains    does the dialog contain activeEl?
 */
function fire(target, activeEl, contains = true) {
  let prevented = false;
  const dialog = { contains: () => contains };
  const e = {
    target, currentTarget: dialog,
    preventDefault: () => { prevented = true; },
  };
  const prior = globalThis.document;
  globalThis.document = { activeElement: activeEl };
  try { keepFocusInDialog(e); } finally { globalThis.document = prior; }
  return prevented;
}

const deadZone = { closest: () => null };                       // a heading, padding
// `closest(INTERACTIVE)` returns the matched ancestor; the guard then asks
// whether a <label> hit actually labels anything.
const control = { closest: () => el("input") };
const deadLabel = { closest: () => el("label") };                  // no htmlFor, wraps nothing
const liveLabel = { closest: () => el("label", { htmlFor: "x", control: el("input") }) };
const wrappingLabel = { closest: () => el("label", { wraps: el("input") }) };
// `htmlFor` pointing at an id that does not exist: labels NOTHING.
const danglingLabel = { closest: () => el("label", { htmlFor: "gone", control: null }) };

test("a click on dead space KEEPS the caret while the operator is typing", () => {
  // The reported bug: 63-66% of a dialog is dead space; a mousedown there moves
  // focus to <body> and every keystroke after it is silently dropped.
  assert.equal(fire(deadZone, textarea), true);
});

test("a click on a real control behaves NORMALLY, so the caret can be placed", () => {
  // Preventing this is the naive fix: it breaks caret placement inside inputs,
  // button activation and label->input association.
  assert.equal(fire(control, textarea), false);
});

test("with nothing being typed, selection is left alone", () => {
  // There is no caret to protect, and a blocker's question must stay selectable.
  assert.equal(fire(deadZone, null), false);
  assert.equal(fire(deadZone, { matches: () => false }), false);
});

test("a caret OUTSIDE currentTarget is still protected — backdrops are siblings", () => {
  // This test previously asserted the OPPOSITE, and in doing so pinned a defect
  // as correct: a backdrop is a SIBLING of its panel, so `contains(active)` is
  // always false there and the guard was a structural no-op. A review reproduced
  // the reported bug through it, in both themes and in the Electron shell.
  assert.equal(fire(deadZone, textarea, /* contains */ false), true);
});

test("a control that cannot take focus must not wave the click through", () => {
  // 🔴 READ THIS BEFORE TRUSTING IT. For a real `disabled` attribute this event
  // CANNOT HAPPEN: Chrome dispatches no mouse events at all on a disabled form
  // control, so the handler is never called and this branch never runs in a
  // browser. An earlier version of this test claimed it covered the reported
  // bug's disabled-control route; it did not, and the product shipped broken
  // through it for six review rounds while this stayed green. That route is
  // fixed in the CSS layer instead (`button:disabled { pointer-events: none }`)
  // and is pinned where it is observable, in `e2e/drawer.mjs`
  // ("[disabled-strand]"), which fails without the rule.
  //
  // The branch is kept because it IS reachable for controls that refuse focus
  // WITHOUT the attribute — `aria-disabled` today takes focus and would strand
  // the caret, and a disabled ancestor `<fieldset>` is one refactor away.
  assert.equal(fire({ closest: () => el("button", { disabled: true }) }, textarea), true);
  assert.equal(fire({ closest: () => el("button") }, textarea), false);
});

test("the backdrop itself still keeps the caret", () => {
  // The original case: currentTarget === target, and `closest` must not be
  // consulted on it (an overlay's closest() would match its own children).
  let prevented = false;
  const dialog = { contains: () => true, closest: () => ({}) };
  const prior = globalThis.document;
  globalThis.document = { activeElement: textarea };
  try {
    keepFocusInDialog({
      target: dialog, currentTarget: dialog,
      preventDefault: () => { prevented = true; },
    });
  } finally { globalThis.document = prior; }
  assert.equal(prevented, true);
});

test("a malformed event never throws", () => {
  assert.doesNotThrow(() => keepFocusInDialog(undefined));
  assert.doesNotThrow(() => keepFocusInDialog({}));
  assert.doesNotThrow(() => keepFocusInDialog({ target: {}, currentTarget: {} }));
});


test("an <input> caret is protected too, not only a <textarea>", () => {
  // Pins TEXT_ENTRY's full selector list. Narrowing it to 'textarea' alone
  // silently drops every input in the product — project name, repo path, the
  // Add-rule Title/Tags fields — and the old fake could not see that.
  assert.equal(fire(deadZone, textInput), true);
});

test("a <label> that labels NOTHING is a heading, and must not strand the caret", () => {
  // Five labels in Settings have no htmlFor and wrap no control. Waving them
  // through as "a real control" is how the reported bug — click a label, lose
  // every following keystroke — survived four review rounds, reproducing in
  // both themes and in the Electron shell.
  assert.equal(fire(deadLabel, textarea), true);
});

test("a <label> that DOES label something keeps normal behaviour", () => {
  // Both ways of labelling: htmlFor, and wrapping the control.
  assert.equal(fire(liveLabel, textarea), false);
  assert.equal(fire(wrappingLabel, textarea), false);
});

test("a <label> whose htmlFor points at nothing must NOT wave the click through", () => {
  // `querySelector` says "no child control" and `htmlFor` says "yes I label
  // something" — only `HTMLLabelElement.control` gets this right, and it is
  // null here. Latent today; a dangling id is one refactor away.
  assert.equal(fire(danglingLabel, textarea), true);
});
