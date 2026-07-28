// A dialog must never silently eat what the operator is typing.
//
// The original report was "the text stopped getting typed there": focus left the
// box and every keystroke after that went nowhere, with the dialog still on
// screen looking perfectly focused. A background re-render was one route to it.
// A MOUSE CLICK is the other, and it is the bigger one — most of a dialog's area
// is not focusable. Measured on the shipped build: 63% of the reply modal and
// 66% of the composer is static text, padding and headings. A mousedown there
// moves focus to <body>, and the next keystroke is lost with no feedback at all.
//
// An earlier version of this guard covered only `e.target === e.currentTarget`,
// i.e. the backdrop — the part of the surface the operator is LEAST likely to
// hit. Three review rounds later the reported symptom still reproduced by
// clicking a heading inside the box, in the browser and in the Electron shell.
//
// The rule, in the order it is applied:
//   1. A real control (input, textarea, button, label, link, [tabindex],
//      [contenteditable]) keeps completely normal behaviour — caret placement,
//      button focus, label→input association. Breaking that is the failure mode
//      of a naive `preventDefault()` on every mousedown.
//   2. Otherwise, if the operator is NOT currently typing inside this dialog, do
//      nothing: there is no caret to protect, and text selection must keep
//      working so a blocker's question can still be selected and copied.
//   3. Only when there IS a caret inside this dialog to lose is the default
//      cancelled — mousedown is what moves focus, so cancelling it leaves the
//      caret exactly where it was.
//
// Step 2 is what stops this trading one bug for another: selection is suppressed
// only while the operator is mid-sentence, which is precisely when they are not
// trying to select static text — and when losing the caret costs them their work.

// `label` is in this list because clicking one must still focus the control it
// labels — but ONLY if it actually labels something. Five labels in Settings
// have no `htmlFor` and wrap no control: they are headings wearing a <label>
// tag, and waving them through is how the reported bug survived four review
// rounds. See `labelsSomething`.
const INTERACTIVE =
  'input, textarea, select, button, label, a[href], [tabindex]:not([tabindex="-1"]), [contenteditable]';

/**
 * Can this element actually TAKE the focus the click is about to move to it?
 *
 * Two ways it cannot, and both stranded the caret in review:
 *  - a DISABLED control. `button`/`input` are in INTERACTIVE, but a disabled one
 *    refuses focus and the browser moves it to <body> instead. There are ~30
 *    `disabled={…}` sites across the guarded dialogs — every busy-gated action
 *    button — and one sits directly under the caret in the spec box.
 *  - a <label> that labels NOTHING: a heading wearing a <label> tag.
 */
function canTakeFocus(el) {
  if (!el) return false;
  if (el.disabled) return false;
  if (el.tagName !== "LABEL") return true;
  // `control` is the DOM's own answer and handles htmlFor pointing at a missing
  // id, which `querySelector` alone gets wrong.
  const labelled = "control" in el
    ? el.control
    : (el.querySelector && el.querySelector("input, textarea, select, button, [contenteditable]"));
  return Boolean(labelled) && !labelled.disabled;
}

const TEXT_ENTRY = 'input, textarea, [contenteditable]';

export function keepFocusInDialog(e) {
  const target = e?.target;
  const dialog = e?.currentTarget;
  if (!target || !dialog) return;

  // 1. Real controls behave normally — but a <label> that labels nothing is not
  //    a control, it is a heading, and clicking it must not strand the caret.
  if (target !== dialog && typeof target.closest === "function") {
    const hit = target.closest(INTERACTIVE);
    if (hit && canTakeFocus(hit)) return;
  }

  // 2. Nothing is being typed → nothing to protect; leave selection alone.
  //
  // 🔴 This deliberately does NOT ask "is the caret inside `currentTarget`?".
  // That question holed the guard in BOTH directions, because a backdrop is a
  // SIBLING of its panel, not an ancestor:
  //   · on `.slideover-backdrop` and `.settings-overlay-backdrop` the answer was
  //     always false, so the guard returned before firing and the whole thing was
  //     a structural no-op — a stray click still dropped every keystroke;
  //   · and with a nested modal open (whose overlay is also a sibling), the
  //     drawer panel did not "contain" the caret either, so dead space in the
  //     drawer stranded the modal's caret too.
  // The app is modal one-at-a-time, so the honest question is simply: is there a
  // caret this click would take away? If so, keep it.
  const active = typeof document !== "undefined" ? document.activeElement : null;
  const typing = Boolean(
    active && typeof active.matches === "function" && active.matches(TEXT_ENTRY),
  );
  if (!typing) return;

  // 3. Keep the caret.
  e.preventDefault();
}

export default keepFocusInDialog;
