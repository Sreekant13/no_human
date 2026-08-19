// Incident 2026-08-12: a websocket that died during a server restart left
// the board silently rendering its last snapshot. `connectionBanner` is the
// pure view-model; these are its logic tests plus the static-source guards
// this repo uses for markup that no jsdom/React renderer can mount
// (sidebarNav.test.mjs, settingsOverlay.test.mjs use the same idiom).
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { connectionBanner } from "./connectionBanner.js";

const here = fileURLToPath(new URL(".", import.meta.url));
const appJsx = readFileSync(here + "App.jsx", "utf8");
const stylesCss = readFileSync(here + "styles.css", "utf8");
const bannerJs = readFileSync(here + "connectionBanner.js", "utf8");

test("the banner renders while disconnected and is absent once live", () => {
  const disconnected = connectionBanner("disconnected");
  assert.ok(disconnected);
  assert.equal(disconnected.text, "Disconnected — data may be stale");
  assert.equal(connectionBanner("live"), null);
});

test("resyncing and sync-failed have their own visible copy", () => {
  const resyncing = connectionBanner("resyncing");
  const syncFailed = connectionBanner("sync-failed");
  const disconnected = connectionBanner("disconnected");
  assert.ok(resyncing);
  assert.ok(syncFailed);
  assert.notEqual(resyncing.text, disconnected.text);
  assert.notEqual(syncFailed.text, disconnected.text);
  assert.notEqual(resyncing.text, syncFailed.text);
});

test("connecting also reads as stale (readyState is not yet OPEN)", () => {
  const connecting = connectionBanner("connecting");
  assert.ok(connecting);
  assert.equal(connecting.text, "Disconnected — data may be stale");
});

test("every phase uses role=\"status\", never an alert() modal", () => {
  for (const phase of ["connecting", "disconnected", "resyncing", "sync-failed"]) {
    const b = connectionBanner(phase);
    assert.equal(b.role, "status");
  }
});

test("App.jsx imports connectionBanner and renders its text/className via role=\"status\", never alert(", () => {
  assert.match(appJsx, /import\s*\{\s*connectionBanner\s*\}\s*from\s*["']\.\/connectionBanner\.js["']/);
  assert.match(appJsx, /connectionBanner\(\s*wsPhase\s*\)/);
  assert.match(appJsx, /banner\.className/);
  assert.match(appJsx, /banner\.text/);
  assert.match(appJsx, /role=\{banner\.role\}/);
  assert.doesNotMatch(appJsx, /alert\(/);
});

test("the fixed-3000ms retry is gone from App.jsx", () => {
  assert.doesNotMatch(appJsx, /setTimeout\(connect,\s*3000\)/);
});

test("styles.css defines .nh-stale-banner and every var(--…) it reads is defined in both themes", () => {
  const raw = stylesCss.replace(/\/\*[\s\S]*?\*\//g, "");
  const bannerBlocks = [...raw.matchAll(/\.nh-stale-banner[a-zA-Z-]*\s*\{([^}]*)\}/g)];
  assert.ok(bannerBlocks.length > 0, ".nh-stale-banner rule must exist");

  const definedInCss = new Set([...raw.matchAll(/(--[a-zA-Z0-9-]+)\s*:/g)].map((m) => m[1]));
  const lightBlock = raw.match(/\[data-theme="light"\]\s*\{([^}]*)\}/)?.[1];
  assert.ok(lightBlock, 'the [data-theme="light"] rule must exist');

  const readVars = new Set();
  for (const [, body] of bannerBlocks) {
    for (const m of body.matchAll(/var\(\s*(--[a-zA-Z0-9-]+)\s*[,)]/g)) readVars.add(m[1]);
  }
  assert.ok(readVars.size > 0, "the banner rules must read at least one CSS var");
  for (const v of readVars) {
    assert.ok(definedInCss.has(v), `${v} is read by .nh-stale-banner but never defined`);
    assert.ok(lightBlock.includes(`${v}:`), `${v} is read by .nh-stale-banner but not overridden for the light theme`);
  }
});

test("the banner is a status strip, not a control surface — it never eats clicks", () => {
  // The behavioural gate for this lives in web/e2e (form-order.mjs goes red
  // without the rule, because a static-server e2e run has no websocket and so
  // renders the banner permanently). CI runs the node suite only — it sets
  // PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD and never invokes web/e2e — so without
  // this assertion the regression is caught only by a human running e2e
  // locally. Cheap here, and it fails for the right reason.
  const raw = stylesCss.replace(/\/\*[\s\S]*?\*\//g, "");
  const base = raw.match(/\.nh-stale-banner\s*\{([^}]*)\}/);
  assert.ok(base, ".nh-stale-banner base rule must exist");
  assert.match(
    base[1],
    /pointer-events\s*:\s*none/,
    ".nh-stale-banner must set pointer-events:none — it is position:fixed over "
      + "the top bar, so without it the strip intercepts every click aimed at "
      + "the controls beneath",
  );
  // The rule is only safe while the banner has nothing to click. If a control
  // is ever added, pointer-events must be restored on it — assert the premise
  // rather than trusting it to stay true.
  assert.doesNotMatch(
    bannerJs,
    /onClick|<button|role:\s*"button"/,
    "the banner gained an interactive element: pointer-events:none now kills "
      + "it, so restore pointer-events:auto on that child",
  );
});
