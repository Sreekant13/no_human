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
import { createRequire } from "node:module";
import path from "node:path";

const HERE = fileURLToPath(new URL(".", import.meta.url));
// The electron PACKAGE's exported binary path, not node_modules/.bin/electron.
// That .bin entry is an extensionless shell script: on Windows the executable
// is electron.cmd and spawning the extensionless name fails with ENOENT, which
// took this whole file out on the first real Windows run. Naming electron.cmd
// instead would not fix it either — Node 22 refuses to spawn .cmd/.bat without
// shell:true (the CVE-2024-27980 mitigation), and turning on a shell to run a
// test harness invites quoting bugs. `require("electron")` returns the real
// electron.exe/electron path on every platform, which is what execFileSync wants.
const ELECTRON = createRequire(import.meta.url)("electron");

const probe = (() => {
  // npm_package_version is DELETED from the probe's env: `npm test` sets it,
  // and the preload's last-resort fallback would then report a real version
  // without the argument — turning the negative control below into a false
  // "the argument is decorative" red. The probe must see what a packaged app
  // sees: no npm env at all.
  const env = { ...process.env };
  delete env.npm_package_version;
  const out = execFileSync(ELECTRON, [path.join(HERE, "testing", "pageProbe.cjs")],
    { encoding: "utf8", timeout: 60000, stdio: ["ignore", "pipe", "ignore"], env });
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
  assert.deepEqual(probe.token.tabOrder,
    ["recheck", "skip-check", "mode-subscription", "mode-api-key", "token", "reveal",
     "codex-subscription", "codex-api-key", "openai-key", "codex-reveal",
     "save", "secondary"],
    "tab order no longer follows reading order (requirements checklist, then required "
    + "Claude, then optional codex, then actions)");
  assert.ok(probe.token.saveMinHeight >= 44,
    `primary action is ${probe.token.saveMinHeight}px; below a 44px touch target`);
});

test("the optional codex section renders, never makes OpenAI required, and reveals the key only for api_key", () => {
  const c = probe.codex;
  assert.ok(c.fieldsetPresent, "the optional codex section did not render");
  assert.equal(c.openaiRequired, false,
    "the OpenAI key must be optional — it must never block Save and start");
  assert.equal(c.openaiType, "password", "the OpenAI key must be masked");
  assert.equal(c.initialKeyRowHidden, true,
    "no codex mode is chosen on open, so the key input must be hidden (skippable)");
  // Subscription = instructions only, NO input (constraint #6b).
  assert.equal(c.sub.keyRowHidden, true, "subscription must expose no key input");
  assert.equal(c.sub.instructionsShown, true, "subscription must show the codex login instructions");
  // api_key reveals the one input and drops the instructions.
  assert.equal(c.key.keyRowHidden, false, "api_key must reveal the key input");
  assert.equal(c.key.instructionsShown, false, "api_key is not the instructions path");
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
  // A plain spawn-timeout is a slow boot, not a credential fault: it gets the
  // honest "still trying to connect" copy in BOTH audiences, never the
  // steps-packaged accusation nor the developer's terminal instructions.
  for (const t of [probe.errTimeout, probe.errTimeoutDev]) {
    assert.ok(t.steps.includes("steps-timeout"),
      "spawn-timeout must show the non-accusatory 'taking longer' block");
    assert.ok(!t.steps.includes("steps-packaged"),
      "spawn-timeout must NOT accuse the credential — the server is merely booting");
    assert.ok(!t.steps.includes("steps-dev"),
      "spawn-timeout is a slow boot, not 'start it in a terminal'");
  }
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

test("the version the preload exposes comes from main's additionalArguments, not a require the sandbox forbids", () => {
  // Measured live on Linux (Settings > Updates read "no_human dev", 2026-08-18):
  // Electron sandboxes preloads by default and a sandboxed preload cannot
  // require("./package.json"), so the old read fell to "dev" in EVERY packaged
  // app. (Display only: the update CHECK itself runs in main on
  // app.getVersion() and was never affected.) The value now travels from main
  // via webPreferences.additionalArguments.
  assert.equal(probe.versionWithArg, "9.9.9-probe",
    "the preload did not pick the version out of --nh-app-version");
  // Negative control: without the argument the sandboxed preload cannot reach
  // package.json and falls back — proving the argument is load-bearing, not
  // decorative. (npm_package_version is deleted from the probe env above.)
  assert.equal(probe.versionWithoutArg, "dev",
    "without the argument the sandboxed preload should have had nothing better than 'dev' — "
    + "if it now finds a real version some other way, update this test to say how");
});
