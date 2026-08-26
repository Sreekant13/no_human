import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// The onboarding telemetry-consent step (onboardingConsent.js) can be
// silently disabled with every other web test staying green, because
// App.jsx:984 `askTelemetry={shouldAskTelemetry(onboardStatus)}` is the ONLY
// wiring that decides whether a real user ever sees it, and nothing observed
// that one prop.
//
// The BEHAVIOURAL authority for this wiring is e2e/onboarding-consent-step.mjs
// (real browser, real built bundle, mocked HTTP). This file exists ONLY
// because CI's web job runs `npm run build` + `npm test` with
// PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 (.github/workflows/ci.yml,
// .gitlab-ci.yml) — no browser layer executes there, so without a
// source-text backstop the wiring would have no gate CI actually runs. Like
// settingsOverlay.test.mjs, `node --test` has no React renderer, so these
// read the .jsx source rather than mounting components.
//
// Attacked by hand before review (mutation matrix): literal `false`/`true`
// for askTelemetry, `setOnboardStatus(s)` deleted from the status-fetch
// `.then`, the `shouldAskTelemetry` import removed / replaced by a same-named
// local stub, the JSX prop reflowed across lines, and the prop reordered
// among Onboarding's other props — each real break goes RED here; reflow and
// reorder (legal, behaviour-preserving edits) stay GREEN.

const here = fileURLToPath(new URL(".", import.meta.url));
const appJsx = readFileSync(here + "App.jsx", "utf8");
const onboardingJsx = readFileSync(here + "Onboarding.jsx", "utf8");

test("App passes the computed consent gate to Onboarding, never a literal", () => {
  assert.match(
    appJsx,
    /askTelemetry=\{\s*shouldAskTelemetry\(\s*onboardStatus\s*\)\s*\}/,
    "askTelemetry must be wired to shouldAskTelemetry(onboardStatus)"
  );
  assert.doesNotMatch(
    appJsx,
    /askTelemetry=\{\s*(true|false|null|undefined)\s*\}/,
    "askTelemetry must never be a hardcoded literal"
  );
});

test("the onboarding status effect stores the full payload the gate reads", () => {
  // Scoped to the fetchOnboardingStatus() effect body specifically — not the
  // whole file — so this cannot be satisfied by an unrelated setOnboardStatus
  // call elsewhere.
  const effectMatch = appJsx.match(/fetchOnboardingStatus\(\)[\s\S]*?\}\s*,\s*\[\s*\]\s*\)\s*;/);
  assert.ok(effectMatch, "the fetchOnboardingStatus() effect was not found");
  const effectBlock = effectMatch[0];
  assert.match(
    effectBlock,
    /setOnboardStatus\(\s*s\s*\)/,
    "the resolved status payload must be stored via setOnboardStatus(s), not dropped"
  );
  assert.match(
    appJsx,
    /const\s*\[\s*onboardStatus\s*,\s*setOnboardStatus\s*\]\s*=\s*useState\(\s*null\s*\)/,
    "onboardStatus/setOnboardStatus must be declared via useState(null)"
  );
});

test("the gate function is imported from onboardingConsent.js", () => {
  assert.match(
    appJsx,
    /import\s*\{\s*shouldAskTelemetry\s*\}\s*from\s*["']\.\/onboardingConsent\.js["']/,
    "shouldAskTelemetry must be imported from onboardingConsent.js, not a local stub"
  );
});

test("Onboarding appends the insights step only under askTelemetry", () => {
  assert.match(
    onboardingJsx,
    /askTelemetry\s*\?\s*\[\s*\.\.\.\s*BASE_STEPS\s*,\s*INSIGHTS_STEP\s*\]\s*:\s*BASE_STEPS/,
    "STEPS must append INSIGHTS_STEP to BASE_STEPS only when askTelemetry is truthy"
  );
  assert.match(
    onboardingJsx,
    /INSIGHTS_STEP\s*=\s*\{\s*title:\s*["']Usage insights["']\s*,\s*key:\s*["']insights["']\s*\}/,
    "INSIGHTS_STEP must keep its title/key shape"
  );
});
