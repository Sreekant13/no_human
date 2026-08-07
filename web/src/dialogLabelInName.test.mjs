import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// WCAG 2.5.3 label-in-name, pinned by something CI actually runs.
//
// WHY THIS EXISTS. A rename of the intake flow shipped once with the dialog's
// aria-label updated and the visible label left on the old wording. A speech-
// input user says what they SEE; if the accessible name does not contain it,
// the control cannot be addressed by voice at all. It was caught by grep, not
// by a test, and the only thing referencing the new web name was an e2e file
// that (a) asserts the aria-label rather than the visible text and (b) is not
// run by CI — ci.yml runs the node unit suite only, because web/e2e needs a
// browser. So the string that had already broken once was the one still
// unpinned. An independent review demonstrated the hole by reverting the
// visible label to the old wording: the whole web suite and the Python guard
// stayed green.
//
// WHY IT DISCOVERS RATHER THAN LISTS. The obvious version pins line 544, or
// pins the literal "Let's scope this". Both go stale the moment someone adds a
// fifth modal or renames the flow again — and a rename is exactly the event
// this protects. So it finds every role="dialog" in App.jsx and checks each one
// that has both an accessible name and a visible label. A new dialog is covered
// the day it is written, by nobody remembering anything.
//
// WHAT IT CANNOT SEE. Only App.jsx, only the `sendback-label` convention, and
// only literal JSX text — a label built from a variable or an i18n call is
// invisible here, as is any dialog whose visible heading uses a different
// class. It reads source, not a render: it proves the two strings agree in the
// file, not that the browser exposes them that way. A `role="dialog"` written
// inside a JSX comment is counted as a dialog, so a comment can pad the
// non-vacuity floor below — it cannot, however, hide a real mismatch.
//
// Attribute ORDER used to matter and no longer does; see the note in dialogs().
// That was a real hole found by review, not a hypothetical, which is why the
// paragraph you are reading now lists what remains rather than claiming none.

const APP = readFileSync(
  fileURLToPath(new URL("./App.jsx", import.meta.url)), "utf8");

const DIALOG = /role="dialog"/g;
const ARIA = /aria-label="([^"]+)"/;
const VISIBLE = /className="sendback-label"\s*>\s*([^<]+?)\s*</;

/** Every role="dialog" occurrence, paired with the accessible name declared on
 *  it and the first visible sendback-label inside it. Segments run to the next
 *  dialog so one modal's label can never be read as another's. */
function dialogs() {
  const starts = [...APP.matchAll(DIALOG)].map((m) => m.index);
  return starts.map((start, i) => {
    const end = i + 1 < starts.length ? starts[i + 1] : APP.length;
    const segment = APP.slice(start, end);
    // The accessible name sits on the SAME element as role="dialog", so read
    // the element's whole opening tag — from the `<` that opens it to the `>`
    // that closes it — and not merely the part that follows role=.
    //
    // The first version of this scanned only `segment[0..indexOf(">")]`, i.e.
    // only attributes written AFTER role="dialog". A review demonstrated the
    // hole by moving aria-label BEFORE role= on the same element: the pair was
    // reported as unlabelled and silently skipped, while the non-vacuity count
    // below still passed on the four dialogs that happened to be ordered the
    // other way. JSX attribute order is arbitrary, so that was a coin flip.
    const tagOpen = APP.lastIndexOf("<", start);
    const tagClose = APP.indexOf(">", start);
    const tag = (tagOpen !== -1 && tagClose !== -1 && tagClose > tagOpen)
      ? APP.slice(tagOpen, tagClose)
      : segment.slice(0, 300);
    const aria = ARIA.exec(tag);
    const visible = VISIBLE.exec(segment);
    return {
      line: APP.slice(0, start).split("\n").length,
      aria: aria && aria[1],
      visible: visible && visible[1],
    };
  });
}

test("the instrument finds the dialogs it claims to check", () => {
  const all = dialogs();
  assert.ok(all.length >= 4,
    `found ${all.length} role="dialog" element(s) in App.jsx — a scan that ` +
    "finds almost nothing reports a clean result for the wrong reason");
  const pairs = all.filter((d) => d.aria && d.visible);
  assert.ok(pairs.length >= 4,
    `found ${pairs.length} dialog(s) with BOTH an accessible name and a ` +
    "visible label; the check below would be nearly vacuous. If the markup " +
    "convention changed, teach this guard the new one — do not delete it.");
});

test("every dialog's accessible name contains its visible label", () => {
  const offenders = [];
  for (const d of dialogs()) {
    if (!d.aria || !d.visible) continue;
    // Case-insensitive on purpose: `aria-label="Refined spec"` beside a visible
    // "Refined Spec" is legitimate and must not be reported. What breaks a
    // speech-input user is DIFFERENT WORDS, not different capitalisation.
    if (!d.aria.toLowerCase().includes(d.visible.toLowerCase())) {
      offenders.push(
        `  App.jsx:${d.line}  visible ${JSON.stringify(d.visible)} is not ` +
        `contained in aria-label ${JSON.stringify(d.aria)}`);
    }
  }
  assert.deepEqual(offenders, [],
    "WCAG 2.5.3 label-in-name: a speech-input user says the words they SEE, " +
    "so the accessible name must contain the visible label:\n" +
    offenders.join("\n"));
});
