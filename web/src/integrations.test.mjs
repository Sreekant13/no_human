import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// The Integrations settings panel now renders an editable Configure form per
// card, generated from the `fields` spec GET /api/integrations returns (PR
// #153's write path). Like themeVars.test.mjs / settingsOverlay.test.mjs, this
// is static source analysis: no jsdom/React renderer is wired into this
// project's `node --test` harness, so these assertions read the .jsx source
// rather than mounting components.

const here = fileURLToPath(new URL(".", import.meta.url));
const src = readFileSync(here + "Integrations.jsx", "utf8");

test("the Configure form is generated from the integration's fields spec", () => {
  assert.match(src, /it\.fields\s*\|\|\s*\[\]/, "must read fields off the card, defaulting to []");
  assert.match(src, /fields\.map\(\(f\)\s*=>/, "must render one row per field in the spec");
  // Each row shows the field's label (never placeholder-only).
  assert.match(src, /<label className="ntm-label"[^>]*>\s*\{f\.label\}/s);
});

test("secret fields render as password inputs with the required placeholder", () => {
  assert.match(src, /type=\{f\.secret \? "password" : "text"\}/);
  assert.match(src, /f\.secret \? \(f\.set \? "●●● set" : "Not set"\) : ""/);
});

test("no field is ever prefilled from server data — every field starts blank", () => {
  // toggleConfigure seeds the whole form to "" regardless of `set`, for both
  // secret and non-secret fields (the GET response never exposes a value for
  // either kind, only `set: bool`).
  assert.match(src, /initial\[f\.name\]\s*=\s*""/,
    "the configure-open handler must seed every field to an empty string");
  // The controlled `value` must come only from local form state, never from
  // the field spec (`f.set` / `f.name` used as a value source would leak or
  // fake a prefill).
  assert.match(src, /value=\{values\[f\.name\]\s*\?\?\s*""\}/,
    "input value must bind only to local formValues state");
  assert.doesNotMatch(src, /value=\{f\.set/, "must never derive the input value from f.set");
});

test("save submits only dirty fields, and an empty secret submit keeps the current value", () => {
  assert.match(src, /function dirtyPayload/);
  assert.match(src, /if\s*\(!dirty\.has\(f\.name\)\)\s*continue/, "untouched fields must be skipped");
  assert.match(src, /if\s*\(f\.secret\s*&&\s*val\s*===\s*""\)\s*continue/,
    "an empty secret submit must be omitted (keep current), never sent as a blank overwrite");
});

test("client-side no-newline validation mirrors the server rule", () => {
  assert.match(src, /handleFieldBlur/);
  assert.match(src, /\/\[\\n\\r\]\//, "must check for newline/carriage-return characters");
});

test("the CI auto-pin note appears for github/gitlab/jenkins/circleci and uses plain, active-voice copy", () => {
  assert.match(src, /CI_AUTOPIN\s*=\s*new Set\(\["github",\s*"gitlab",\s*"jenkins",\s*"circleci"\]\)/);
  assert.match(src, /active CI\s*\n?\s*.*backend/, "must state it becomes the active CI backend");
  assert.match(src, /turns CI on/i, "must state CI gets enabled, in plain language");
  // No raw config-key jargon in the visible copy string itself.
  assert.doesNotMatch(src, /Saving here makes[^<]*ci\.(backend|enabled)/,
    "the visible note text must not contain raw config keys like ci.backend/ci.enabled");
});

test("a helper hint exists for jargon field names, e.g. org_slug", () => {
  assert.match(src, /org_slug:\s*"/);
  assert.match(src, /FIELD_HELP\[f\.name\]/);
});

test("Save is disabled while saving, on validation errors, or with nothing dirty to send", () => {
  assert.match(src, /disabled=\{saving \|\| hasErrors \|\| !hasChanges\}/);
});

test("Test connection is disabled during its own check AND while a save is in flight", () => {
  assert.match(src, /disabled=\{testing === it\.name \|\| saving === it\.name\}/);
});

test("fetchIntegrations in load() has a catch guard (no unhandled-rejection / stuck spinner)", () => {
  assert.match(src, /fetchIntegrations\(\)/);
  assert.match(src, /\.then\(\(r\) => setItems\(r\.integrations \|\| \[\]\)\)\s*\n\s*\.catch\(\(\) => setItems\(\[\]\)\)/,
    "load() must chain a .catch right after fetchIntegrations().then(...)");
});

test("a successful save merges the refreshed status+fields back into the card list", () => {
  assert.match(src, /saveIntegrationConfig\(it\.name,\s*payload\)/);
  assert.match(src, /setItems\(\(prev\)\s*=>\s*prev\.map\(\(x\)\s*=>\s*\(x\.name === it\.name \? \{ \.\.\.x, \.\.\.refreshed \} : x\)\)\)/);
});

// ── Review-fix pins (dual-review Minor findings on the Configure form) ─────

test("required-field validation fires only for non-secret fields the user dirtied and then emptied", () => {
  assert.match(src, /function handleFieldBlur\(name,\s*value,\s*secret\)/,
    "blur handler must receive the field's secret flag alongside name/value");
  assert.match(src, /!secret\s*&&\s*dirty\.has\(name\)\s*&&\s*value\s*===\s*""/,
    "required rule: non-secret, previously-dirtied, now-empty — never on first open, never for secrets");
  assert.match(src, /This field is required\./, "must show a visible required-field message");
  // Still wired to the field's secret flag at the call site, not hardcoded.
  assert.match(src, /onBlur\(f\.name,\s*e\.target\.value,\s*f\.secret\)/);
});

test("inputs wire aria-invalid and aria-describedby to their helper/error element ids", () => {
  assert.match(src, /aria-invalid=\{fieldErrors\[f\.name\]\s*\?\s*"true"\s*:\s*undefined\}/);
  assert.match(src, /aria-describedby=\{describedBy\}/);
  // The ids it points at are actually rendered on the hint/error elements.
  assert.match(src, /const hintId = FIELD_HELP\[f\.name\]\s*\?\s*`\$\{inputId\}-hint`\s*:\s*null/);
  assert.match(src, /const errorId = fieldErrors\[f\.name\]\s*\?\s*`\$\{inputId\}-error`\s*:\s*null/);
  assert.match(src, /id=\{hintId\}/);
  assert.match(src, /id=\{errorId\}/);
});

test("the save-error message is an aria-live=polite region and a focus target", () => {
  assert.match(src, /className="new-task-error"\s+aria-live="polite"/,
    "the save error must announce to assistive tech as it appears");
  assert.match(src, /ref=\{errorRef\}/, "the error region must be focusable as the no-field-error fallback");
});

test("after a failed save, focus moves to the first invalid field, else the error region", () => {
  assert.match(src, /const firstInvalid = fields\.find\(\(f\)\s*=>\s*fieldErrors\[f\.name\]\)/);
  assert.match(src, /\(target \|\| errorRef\.current\)\?\.focus\(\)/);
});

test("Escape closes the expanded card AND, if a Configure form is open, closes it and wipes formValues", () => {
  assert.match(src, /const closeOnEscape = useCallback\(\(\) => \{/,
    "escape handler must be a single function so it can reset every configure-form field");
  assert.match(src, /setExpanded\(null\);\s*\n\s*setConfiguring\(null\);\s*\n\s*setFormValues\(\{\}\);/,
    "escape must close the configure form (setConfiguring(null)) and wipe typed values (setFormValues({}))");
  assert.match(src, /useEscapeKey\(closeOnEscape,\s*expanded !== null\)/);
});
