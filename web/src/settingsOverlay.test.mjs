import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// Settings moved from a routed page (page === "settings") to an overlay dialog
// with a left section list, modeled on the Claude macOS desktop app — and the
// Config tab/panel was removed entirely (the backend bug it exposed is fixed
// separately in tests/test_api_config_scrub.py). Like themeVars.test.mjs and
// tokenBridge.test.mjs, this is static source analysis: no jsdom/React
// renderer is wired into this project's `node --test` harness, so these
// assertions read the .jsx source rather than mounting components.

const here = fileURLToPath(new URL(".", import.meta.url));
const settingsJsx = readFileSync(here + "Settings.jsx", "utf8");
const appJsx = readFileSync(here + "App.jsx", "utf8");
const stylesCss = readFileSync(here + "styles.css", "utf8");

test("the Config section is gone: no ConfigPanel, no config entry in the section list", () => {
  assert.doesNotMatch(settingsJsx, /ConfigPanel/, "ConfigPanel must be removed");
  assert.doesNotMatch(settingsJsx, /fetchConfig/, "fetchConfig import/usage must be removed");
  // The section list (formerly TABS) must not carry a "config" key or "Config" label.
  assert.doesNotMatch(settingsJsx, /key:\s*["']config["']/);
  assert.doesNotMatch(settingsJsx, /label:\s*["']Config["']/);
});

test("the section list covers exactly Projects, Rules, Skills, Learnings, Integrations", () => {
  for (const label of ["Projects", "Rules", "Skills", "Learnings", "Integrations"]) {
    assert.match(settingsJsx, new RegExp(`label:\\s*["']${label}["']`), `missing section: ${label}`);
  }
});

test("Settings renders as an overlay (backdrop + dialog), not a bare page", () => {
  assert.match(settingsJsx, /role="dialog"/);
  assert.match(settingsJsx, /aria-modal="true"/);
  // A backdrop element that closes the overlay on click.
  assert.match(settingsJsx, /backdrop/i);
});

test("the overlay closes on Escape", () => {
  assert.match(settingsJsx, /["']Escape["']/);
});

test("App.jsx no longer routes Settings as a page — it drives a settingsOpen overlay state", () => {
  assert.doesNotMatch(appJsx, /page === "settings"/);
  assert.match(appJsx, /settingsOpen/);
});

// Review fix #3: the trigger that opened the overlay gets focus back on close
// (Escape, ×, backdrop all tear down the component the same way, so a single
// mount/unmount effect covers all three paths).
test("the overlay captures the trigger element on open and restores focus to it on unmount", () => {
  assert.match(settingsJsx, /triggerRef\.current\s*=\s*document\.activeElement/,
    "must capture document.activeElement (the trigger) when the overlay opens");
  // The restore must run from a cleanup function (mount/unmount effect), not
  // from the Escape/close handlers directly — that's what makes it fire for
  // Escape, the × button, and the backdrop alike.
  assert.match(settingsJsx, /return\s*\(\)\s*=>\s*\{[^}]*triggerRef\.current[^}]*\.focus\(\)/s,
    "must restore focus to the captured trigger in an effect cleanup");
  // Guard against the trigger having been unmounted before the overlay closes.
  assert.match(settingsJsx, /document\.contains\(trigger\)/,
    "must guard against the trigger no longer being in the DOM");
});

// Review fix #4: section nav buttons expose aria-current when active.
test("active section nav buttons get aria-current=\"true\"", () => {
  assert.match(settingsJsx, /aria-current=\{section === s\.key \? "true" : undefined\}/,
    "nav buttons must set aria-current=\"true\" only for the active section");
});

// Review fix #1: the overlay header (Settings.jsx) is the single title; each
// panel's own in-body heading text is scope-hidden under the overlay
// container rather than deleted, so panels stay intact if reused elsewhere.
test("panel section titles are scoped-hidden inside the overlay, not duplicated", () => {
  const titleTextOccurrences = (settingsJsx.match(/panel-title-text/g) || []).length;
  // ProjectsPanel, MemoryList (rules+skills share one component), LearningsPanel.
  assert.ok(titleTextOccurrences >= 3,
    `expected the panel-title-text marker on at least 3 panel headings, found ${titleTextOccurrences}`);
  assert.match(stylesCss, /\.settings-overlay-body\s+\.panel-title-text\s*\{[^}]*display:\s*none/,
    "styles.css must scope-hide .panel-title-text under .settings-overlay-body");
  // The count/toggle siblings (e.g. .memory-count) must stay untouched — only
  // the redundant text label is hidden, not the whole heading.
  assert.doesNotMatch(stylesCss, /\.settings-overlay-body\s+\.memory-title\s*\{[^}]*display:\s*none/,
    "must not hide the whole heading (that would also hide the count badge)");
});
