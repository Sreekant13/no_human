import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// The onboarding usage-insights CONSENT step was REMOVED (operator, 2026-08-26):
// telemetry is ON by default and no longer asked about, so the wizard must not
// carry a consent step, an `askTelemetry` gate, or any usage-insights mention.
//
// This file used to guard that the consent step was WIRED IN. It now guards the
// opposite invariant — that it stays OUT — so a later edit cannot quietly
// re-introduce a step the operator decided against. Like settingsOverlay.test.mjs,
// `node --test` has no React renderer, so these read the .jsx source rather than
// mounting components.
//
// The three ways the removal can regress, each covered below: App re-passing an
// `askTelemetry` prop, App re-importing/computing `shouldAskTelemetry`, and
// Onboarding re-introducing the INSIGHTS_STEP / insights step key.

const here = fileURLToPath(new URL(".", import.meta.url));
const appJsx = readFileSync(here + "App.jsx", "utf8");
const onboardingJsx = readFileSync(here + "Onboarding.jsx", "utf8");

test("App no longer passes any askTelemetry prop to Onboarding", () => {
  assert.doesNotMatch(
    appJsx,
    /askTelemetry\s*=/,
    "the removed consent step must not be re-wired via an askTelemetry prop"
  );
  // The <Onboarding> render must still exist — this is a removal, not a break.
  assert.match(appJsx, /<Onboarding\b/, "App must still render <Onboarding>");
});

test("the onboarding status effect only records completion, not a consent payload", () => {
  const effectMatch = appJsx.match(/fetchOnboardingStatus\(\)[\s\S]*?\}\s*,\s*\[\s*\]\s*\)\s*;/);
  assert.ok(effectMatch, "the fetchOnboardingStatus() effect was not found");
  const effectBlock = effectMatch[0];
  assert.match(
    effectBlock,
    /setOnboarded\(/,
    "the effect must still set the onboarded gate"
  );
  assert.doesNotMatch(
    appJsx,
    /onboardStatus/,
    "the consent-only onboardStatus state must be gone, not merely unread"
  );
});

test("App does not import or compute the removed consent gate", () => {
  assert.doesNotMatch(
    appJsx,
    /shouldAskTelemetry/,
    "shouldAskTelemetry belonged to the removed step and must not be referenced in App"
  );
});

test("Onboarding has no insights step, and STEPS is just BASE_STEPS", () => {
  assert.doesNotMatch(
    onboardingJsx,
    /INSIGHTS_STEP/,
    "the INSIGHTS_STEP definition must be gone"
  );
  assert.doesNotMatch(
    onboardingJsx,
    /key: "insights"/,
    "no insights step key may remain in the wizard"
  );
  assert.match(
    onboardingJsx,
    /const STEPS = BASE_STEPS;/,
    "STEPS must be the fixed 8-step BASE_STEPS with nothing appended"
  );
  assert.doesNotMatch(
    onboardingJsx,
    /askTelemetry/,
    "the askTelemetry prop and every use of it must be gone"
  );
});
