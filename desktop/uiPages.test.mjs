// Behavioural coverage for the two pages this branch ships.
//
// These are computed-style and DOM questions — whether a focus ring is actually
// painted, whether aria-pressed flips, whether [hidden] really hides — so they
// are measured in a REAL renderer. A regex over the stylesheet would happily
// pass a rule that never applies, and pattern-matching UI behaviour has cost
// this repo entire review rounds before.
import assert from "node:assert/strict";
import test from "node:test";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const ELECTRON = path.join(HERE, "node_modules", ".bin", "electron");

const probe = (() => {
  const out = execFileSync(ELECTRON, [path.join(HERE, "testing", "pageProbe.cjs")],
    { encoding: "utf8", timeout: 60000, stdio: ["ignore", "pipe", "ignore"] });
  const line = out.split("\n").find((l) => l.startsWith("PROBE_JSON:"));
  assert.ok(line, `the page probe produced no measurement:\n${out.slice(0, 400)}`);
  return JSON.parse(line.slice("PROBE_JSON:".length));
})();

test("the credential field is focused on open and keeps a visible focus ring", () => {
  assert.equal(probe.autofocused, "token",
    "this screen exists to collect one value; it must be focused on open");
  const ring = probe.token.focusRing;
  assert.equal(ring.on, "token", "Tab did not land on the credential field first");
  assert.equal(ring.matches, true, "the field does not match :focus-visible");
  assert.ok(parseFloat(ring.width) >= 2 && ring.style !== "none",
    `keyboard focus must be visible; got ${ring.width} ${ring.style}`);
});

test("the reveal toggle flips both the input type and aria-pressed", () => {
  const { before, after } = probe.token;
  assert.deepEqual(before, { type: "password", pressed: "false" },
    "the credential must be masked by default");
  assert.deepEqual(after, { type: "text", pressed: "true" },
    "a toggle button that never updates aria-pressed lies to a screen reader");
});

test("a rejected save marks the field invalid and announces it", () => {
  assert.equal(probe.token.ariaInvalid, "true",
    "the field was not flagged, so the error is visual-only");
  assert.equal(probe.token.msgRole, "alert", "the message must be announced");
  assert.ok(probe.token.msgText.length > 0, "the alert region was left empty");
  assert.equal(probe.token.labelFor, "token", "the field has no programmatic label");
  assert.deepEqual(probe.token.tabOrder, ["token", "reveal", "save", "secondary"],
    "tab order no longer follows reading order");
  assert.ok(probe.token.saveMinHeight >= 44,
    `primary action is ${probe.token.saveMinHeight}px; below a 44px touch target`);
});

test("error.html routes each reason to the right guidance", () => {
  assert.ok(probe.errStopFailed.steps.includes("steps-stopfailed"),
    "a stop-failed server showed generic advice instead of its own");
  assert.ok(probe.errPackaged.steps.includes("steps-packaged"),
    "a packaged user was not shown the friend-facing copy");
  assert.ok(probe.errDev.steps.includes("steps-dev"),
    "a developer was not shown the developer copy");
  assert.ok(!probe.errPackaged.steps.includes("steps-dev"),
    "a friend on a DMG was told to run `uv tool install` from a git checkout");
});

test("the token link hides only when nh itself is missing", () => {
  assert.equal(probe.errNotFound.tokenLinkVisible, false,
    "with no nh binary the credential is not the problem; offering it misleads");
  assert.equal(probe.errPackaged.tokenLinkVisible, true,
    "the only route back to the credential screen disappeared");
  assert.equal(probe.errStopFailed.tokenLinkVisible, true);
});

test("[hidden] actually hides the retry link", () => {
  // a.retry sets display:block, which overrides the UA [hidden] rule unless the
  // page restores it — a real prior defect: the link stayed clickable while
  // marked hidden.
  assert.equal(probe.errPackaged.retryDisplayShown, "block");
  assert.equal(probe.errPackaged.retryDisplayWhenHidden, "none",
    "a hidden retry link was still displayed and clickable");
});

test("both shipped pages declare a language", () => {
  assert.equal(probe.token.lang, "en");
  assert.equal(probe.errPackaged.lang, "en");
});
