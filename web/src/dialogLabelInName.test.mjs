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
// only literal JSX text — a label built from a variable, an i18n call, or a
// `role={"dialog"}` expression is invisible here, as is any dialog whose
// visible heading uses a different class. It reads source, not a render: it
// proves the two strings agree in the file, not that the browser exposes them
// that way.
//
// A `role="dialog"` written inside a JSX comment IS counted as a dialog. That
// cuts both ways and the earlier version of this paragraph got it wrong in both
// directions: a comment can pad the non-vacuity floor below, AND a commented
// mismatched pair can invent a violation that does not ship.
//
// TWO CORRECTIONS ARE RECORDED HERE RATHER THAN QUIETLY FIXED, because each was
// a claim this file made about itself that was false when written:
//   * v1 read only attributes written AFTER role=, so reordering hid a pair.
//   * v2 said it read "the whole opening tag" and did not — it stopped at the
//     nearest `>`, which is not the tag's end when a prop contains one. Two
//     review mutants survived on that, and with one more legitimate dialog
//     padding the >= 4 floor, a real WCAG violation shipped green.
// Both are closed by tagEnd() above. State what the scanner does, not what it
// was meant to do.

const APP = readFileSync(
  fileURLToPath(new URL("./App.jsx", import.meta.url)), "utf8");

const DIALOG = /role="dialog"/g;

/** Index just past the `>` that really closes the JSX tag opening at `from`.
 *
 *  `indexOf(">")` is wrong: a `>` inside a quoted attribute value
 *  (`aria-label="a > b"`) or inside a JSX expression (`onClick={() => f()}`)
 *  closes nothing. Truncating there loses the rest of the attributes, and the
 *  guard then reports the dialog as UNLABELLED and skips it — which is
 *  indistinguishable from "there is no dialog here", the silent failure this
 *  whole file exists to prevent. So track quotes and brace depth. */
function tagEnd(src, from) {
  let quote = null, depth = 0;
  for (let i = from; i < src.length; i++) {
    const c = src[i];
    if (quote) { if (c === quote) quote = null; continue; }
    if (c === '"' || c === "'") { quote = c; continue; }
    if (c === "{") { depth++; continue; }
    if (c === "}") { if (depth > 0) depth--; continue; }
    if (c === ">" && depth === 0) return i;
  }
  return src.length;
}
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
    // the element's whole opening tag and not merely the part that follows
    // role=. Two corrections are baked in here, both from real review findings:
    //
    // 1. The FIRST version scanned only `segment[0..indexOf(">")]` — attributes
    //    written AFTER role="dialog". Moving aria-label BEFORE role= made the
    //    pair read as unlabelled and SILENTLY SKIPPED, while the non-vacuity
    //    floor still passed on the dialogs ordered the other way.
    // 2. The SECOND version claimed to read "from the `<` that opens it to the
    //    `>` that closes it" and did not: it took the nearest following `>`,
    //    which is NOT the tag's end whenever a JSX expression in the same tag
    //    contains one — `onClick={() => f()}` is idiomatic React and App.jsx
    //    holds 86 arrow functions. The tag was truncated mid-attribute, aria
    //    came back null, and the pair was skipped again. Today's dialogs merely
    //    happen to put aria-label before any such prop; that is an accident,
    //    not a property.
    //
    // So find the tag's real end: the first `>` that is not inside a quoted
    // attribute value and not inside a `{…}` expression. A skipped pair is the
    // dangerous failure here, because it looks identical to "no dialog".
    const tagOpen = APP.lastIndexOf("<", start);
    const tag = tagOpen === -1 ? segment.slice(0, 300)
      : APP.slice(tagOpen, tagEnd(APP, tagOpen));
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
