import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { EVENT_LABELS, eventLabel } from "./eventLabels.js";

const DIST_ASSETS = join(dirname(fileURLToPath(import.meta.url)), "..", "dist", "assets");

// These replace a source-text guard in
// tests/test_wake_comment_conflict_precedence.py that read SlideOver.jsx as a
// string and asserted `"pr_feedback_deferred:" in text`. That assertion passed
// with the mapping COMMENTED OUT — verified by mutation on 2026-08-22 — so the
// board could fall back to the raw kind with the suite green.
//
// COMPLETENESS is NOT checked here. `doctor.py`'s MECHANISMS table is the
// authoritative list of the kinds the PR-watch ladder emits, and it lives in
// Python; the pytest test `test_every_pr_watch_ladder_kind_has_a_board_label`
// owns that check so it cannot drift. What this file owns is the module's
// behaviour and the fact that the built board actually uses it.

test("pr_feedback_deferred renders as a human label, not the raw kind", () => {
  assert.equal(eventLabel("pr_feedback_deferred"), "PR feedback deferred");
});

test("a sample of PR-watch kinds resolve to labels (completeness lives in pytest)", () => {
  for (const kind of ["merged", "pr_closed", "pr_feedback_deferred", "escalated_ci"]) {
    assert.ok(
      Object.prototype.hasOwnProperty.call(EVENT_LABELS, kind),
      `${kind} has no label, so the board would show the raw kind`,
    );
    assert.notEqual(eventLabel(kind), kind, `${kind} resolves to itself`);
  }
});

test("every label is a usable string — runtime-exact, so source-text tricks cannot pass", () => {
  // This check lives in JS because only JS decodes the value the board will
  // actually render. A Python regex over this file reads SOURCE TEXT, so it
  // passes on `"\t"`, `"\u200b"` and `"\n"` (non-empty in source, blank on the
  // board) and cannot see an `Object.assign(EVENT_LABELS, ...)` override after
  // the literal at all — an independent reviewer defeated the earlier pytest
  // version with exactly those on 2026-08-22. Here the map is imported, so the
  // value is the decoded, post-override, post-duplicate-resolution string.
  for (const [kind, label] of Object.entries(EVENT_LABELS)) {
    // `.trim()` alone is NOT enough: it strips Unicode White_Space, and a
    // zero-width space (U+200B) is a FORMAT character, not whitespace, so
    // `"\u200b".trim()` returns it unchanged and reads as a real label while
    // the board renders an invisible chip. Found by mutation on 2026-08-22.
    // Require at least one character that is neither whitespace nor format.
    assert.ok(
      typeof label === "string" && /[^\s\p{Cf}]/u.test(label) && label !== kind,
      `${kind}: unusable label ${JSON.stringify(label)} — the board would render `
      + "a blank chip or the raw kind",
    );
  }
});

test("an unknown kind still falls back to itself", () => {
  // The fallback is deliberate: a new backend kind must not blank the row.
  assert.equal(eventLabel("some_kind_added_later"), "some_kind_added_later");
});

// Moving the map OUT of SlideOver.jsx created a decoupling surface that did not
// exist before: the module can be correct and fully tested while the component
// no longer imports it, and every label silently disappears from the board.
// Verified by mutation on 2026-08-22 — dropping the import and defining a local
// `eventLabel(kind) { return kind; }` left the whole JS suite green (1117/1117)
// and the vite build clean, while every label vanished from the bundle. Only
// the built artifact can see that, so this asserts on the artifact.
//
// KNOWN LIMIT, stated so nobody reads more into a pass than it carries: this
// establishes that the BUNDLER RETAINED THE MAP, which is strictly weaker than
// "the board is wired up". A reviewer defeated it on 2026-08-22 by leaving the
// import in place (it is used at SlideOver.jsx:1823) and breaking only the
// timeline chip at :1281, `{eventLabel(kind)}` -> `{kind}`: the timeline then
// renders raw snake_case with all three labels still present in the bundle and
// the suite green. Closing that needs a JSX-capable runner rendering the
// component, which this repo does not have. Note this is NOT a hole the
// extraction opened — the same edit sailed past the source-text guard that
// preceded it.
// Fails CLOSED — an absent/empty dist reads as "can't verify", not "clean".
test("the built board bundle retains the label map (weaker than: the board is wired up — see KNOWN LIMIT above)", () => {
  if (!existsSync(DIST_ASSETS)) {
    assert.fail("web/dist/assets is missing — run `npm run build` in web/ first");
  }
  const jsFiles = readdirSync(DIST_ASSETS).filter((f) => f.endsWith(".js"));
  if (jsFiles.length === 0) {
    assert.fail("web/dist/assets has no JS asset — run `npm run build` in web/ first");
  }
  const bundle = jsFiles.map((f) => readFileSync(join(DIST_ASSETS, f), "utf8")).join("\n");

  for (const label of ["PR feedback deferred", "CI escalated", "Knowledge applied"]) {
    assert.ok(
      bundle.includes(label),
      `"${label}" is not in the built bundle. Either no bundled module imports `
      + "./eventLabels.js any more (so the bundler shook the map out and the board "
      + "renders raw kinds), or the label was removed from the map.",
    );
  }
});
