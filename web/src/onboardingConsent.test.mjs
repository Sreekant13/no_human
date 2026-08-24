import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  TELEMETRY_CONSENT_QUESTION,
  TELEMETRY_CONSENT_SETTINGS_HINT,
  CONSENT_YES_LABEL,
  CONSENT_NO_LABEL,
  shouldAskTelemetry,
  submitConsent,
} from "./onboardingConsent.js";

// Onboarding never asked about telemetry, so the toggle sat buried in Settings
// and no real install ever showed up in a replay. This is the one-time,
// default-No consent step added after the existing 8 onboarding steps. No new
// write path: `submitConsent` only ever calls the `saveTelemetryConsent` it is
// handed (the existing PUT /api/telemetry/consent wrapper in api.js). As with
// onboardingNav.test.mjs, `node --test` has no React renderer, so the decision
// logic lives in this plain module and the JSX wiring is checked by reading
// Onboarding.jsx's source text (the ruleTextFull.test.mjs idiom).

// ── the decision logic ──────────────────────────────────────────────────────

test("skip (never touched the buttons) persists asked and enables nothing", async () => {
  let called = false;
  const saveTelemetryConsent = async () => { called = true; };
  const result = await submitConsent(null, { saveTelemetryConsent });
  assert.equal(called, false, "skipping must never call the consent endpoint");
  assert.equal(result.called, false);
  assert.equal(result.enabled, false);
  assert.equal(result.telemetryAsked, true, "a skip still counts as asked — it must not re-nag");
});

test("an explicit No persists asked and enables nothing", async () => {
  let called = false;
  const saveTelemetryConsent = async () => { called = true; };
  const result = await submitConsent(false, { saveTelemetryConsent });
  assert.equal(called, false);
  assert.equal(result.enabled, false);
  assert.equal(result.telemetryAsked, true);
});

test("Yes calls the existing consent endpoint exactly once, with true", async () => {
  const calls = [];
  const saveTelemetryConsent = async (enabled) => { calls.push(enabled); };
  const result = await submitConsent(true, { saveTelemetryConsent });
  assert.deepEqual(calls, [true], "must reuse the existing wrapper, not a new write path");
  assert.equal(result.enabled, true);
  assert.equal(result.telemetryAsked, true);
});

test("a consent failure does not block the launch, and does not lose the Yes forever", async () => {
  const saveTelemetryConsent = async () => { throw new Error("network down"); };
  let errored;
  const result = await submitConsent(true, {
    saveTelemetryConsent,
    onError: (e) => { errored = e; },
  });
  assert.equal(result.enabled, false);
  assert.ok(errored, "the failure must be surfaced, not swallowed silently");
  // The write failed (a possibly-transient fault) — it must NOT be recorded
  // as "asked". If it were, shouldAskTelemetry() would return false forever:
  // the user said Yes, telemetry stayed off, and they would never be offered
  // the choice again. Leaving telemetryAsked false here means the complete
  // payload omits the key, so the next launch asks again instead of silently
  // losing the Yes (see onboarding_complete's sticky patch in app.py).
  assert.equal(result.telemetryAsked, false, "a failed write must not be recorded as asked — re-ask next launch");
});

test("shouldAskTelemetry re-asks after a failed Yes, since it was never actually recorded", () => {
  // Mirrors the shape onboarding_complete persists when telemetryAsked was
  // false: the "telemetry_asked" key is simply absent from onboarding state.
  assert.equal(shouldAskTelemetry({ completed: true }), true);
});

test("an install that was already asked never sees the step again", () => {
  assert.equal(shouldAskTelemetry({ telemetry_asked: true }), false);
  assert.equal(shouldAskTelemetry({ completed: true, telemetry_asked: true }), false);
});

test("an install that has never been asked sees the step", () => {
  assert.equal(shouldAskTelemetry({ completed: true }), true);
  assert.equal(shouldAskTelemetry({ completed: false }), true);
  assert.equal(shouldAskTelemetry(null), true);
  assert.equal(shouldAskTelemetry(undefined), true);
});

// ── the copy: byte-identical twin of config.py's contract ──────────────────
// tests/test_telemetry.py pins the Python side of this same equality, so a
// comment edit there and a copy edit here cannot silently part ways.

test("the question names exactly what is collected and nothing more", () => {
  assert.match(TELEMETRY_CONSENT_QUESTION, /anonymous usage events/);
  assert.match(TELEMETRY_CONSENT_QUESTION, /masked screen recordings/);
  assert.match(
    TELEMETRY_CONSENT_QUESTION,
    /never code, prompts, titles, paths or tokens/,
    "must mirror config.py's own telemetry-block comment, not a paraphrase",
  );
});

test("the settings hint points at the real place to change it later", () => {
  assert.match(TELEMETRY_CONSENT_SETTINGS_HINT, /Settings > Usage insights/);
});

test("no dark patterns in the button copy", () => {
  // Plainly-worded yes/no — not "Accept"/"Maybe later", not a checkbox label.
  assert.equal(CONSENT_NO_LABEL, "No");
  assert.doesNotMatch(CONSENT_YES_LABEL, /accept|agree|allow all/i);
});

// ── the wiring: Onboarding.jsx actually renders this, once, after Launch ───

const here = fileURLToPath(new URL(".", import.meta.url));
const jsx = readFileSync(here + "Onboarding.jsx", "utf8");
const api = readFileSync(here + "api.js", "utf8");

test("the insights step is appended after the existing 8 steps, not spliced in", () => {
  const base = jsx.match(/const BASE_STEPS = \[([\s\S]*?)\n\];/);
  assert.ok(base, "the original 8-step list must still exist, untouched, as its own array");
  const keys = [...base[1].matchAll(/key: "(\w+)"/g)].map((m) => m[1]);
  assert.deepEqual(
    keys,
    ["welcome", "repos", "projects", "docs", "integrations", "history", "rules", "summary"],
    "the existing 8 steps must not be reordered or renamed",
  );
  assert.match(
    jsx,
    /const INSIGHTS_STEP = \{[^}]*key: "insights"[^}]*\}/,
    "a distinct insights step must be defined",
  );
  assert.match(
    jsx,
    /askTelemetry \? \[\.\.\.BASE_STEPS, INSIGHTS_STEP\] : BASE_STEPS/,
    "insights must be appended after the 8, and only conditionally on askTelemetry",
  );
});

test("the step renders only for an install that has never been asked", () => {
  assert.match(
    jsx,
    /export default function Onboarding\(\{ onComplete, askTelemetry \}\)/,
    "the decision must come from a prop, not be recomputed inside the component",
  );
});

test("No is the pre-selected default, matching the product's off-by-default telemetry", () => {
  assert.match(
    jsx,
    /const \[consent, setConsent\] = useState\(false\)/,
    "consent state must default to false (No)",
  );
});

test("the insights step prints the pinned copy and offers No / Yes", () => {
  const start = jsx.indexOf('{step.key === "insights" &&');
  assert.ok(start > 0, "the insights step block must exist");
  const end = jsx.indexOf("{err &&", start);
  assert.ok(end > start, "could not bound the insights step");
  const block = jsx.slice(start, end);

  assert.match(block, /\{TELEMETRY_CONSENT_QUESTION\}/, "must print the pinned question, not inline text");
  assert.match(block, /\{TELEMETRY_CONSENT_SETTINGS_HINT\}/, "must print the pinned settings hint, not inline text");

  assert.doesNotMatch(
    block,
    /type="checkbox"/,
    "no pre-checked (or any) checkbox — this is a plain two-button choice",
  );

  // No is the highlighted/default control (ob-btn, the primary-button class
  // used elsewhere for Continue); Yes is the secondary ob-btn-ghost (used
  // elsewhere for Back). Static per-role classes, not swapped on click, so
  // this assertion is reliable without a renderer.
  const noBtn = block.match(/<button[^>]*onClick=\{\(\) => setConsent\(false\)\}[^>]*>/s)
    || block.match(/className="ob-btn"[\s\S]*?onClick=\{\(\) => setConsent\(false\)\}/);
  assert.ok(noBtn, "No button must exist and set consent to false");
  assert.match(block, /className="ob-btn"[\s\S]{0,80}?onClick=\{\(\) => setConsent\(false\)\}/,
    "No must carry the highlighted/default button class");
  assert.match(block, /className="ob-btn-ghost"[\s\S]{0,80}?onClick=\{\(\) => setConsent\(true\)\}/,
    "Yes must carry the secondary button class, not the default one");

  assert.doesNotMatch(block, /autoFocus/, "neither button should steal focus into a nag");
});

test("no new CSS classes were introduced for the step", () => {
  const start = jsx.indexOf('{step.key === "insights" &&');
  const end = jsx.indexOf("{err &&", start);
  const block = jsx.slice(start, end);
  const classNames = [...block.matchAll(/className="([^"]+)"/g)].map((m) => m[1]);
  for (const c of classNames) {
    for (const cls of c.split(" ")) {
      assert.match(
        cls,
        /^ob-(btn|btn-ghost|h2|note|row|nav-spacer)$/,
        `unexpected new class "${cls}" — reuse the wizard's existing classes only`,
      );
    }
  }
});

test("the wizard reuses api.js's consent call — no new write path", () => {
  assert.match(jsx, /saveTelemetryConsent/, "must import the existing wrapper");
  assert.doesNotMatch(
    jsx,
    /fetch\(.*telemetry\/consent/,
    "must not talk to the endpoint directly, bypassing api.js",
  );
  const hits = [...api.matchAll(/\/api\/telemetry\/consent/g)];
  assert.equal(hits.length, 1, "api.js must still define the endpoint exactly once — no new path added");
});

test("a telemetry failure cannot block completeOnboarding", () => {
  const start = jsx.indexOf("async function finish()");
  assert.ok(start > 0);
  const end = jsx.indexOf("\n  }\n", start);
  const body = jsx.slice(start, end);
  const consentIdx = body.indexOf("submitConsent(");
  const completeIdx = body.indexOf("completeOnboarding(");
  assert.ok(consentIdx > 0 && completeIdx > consentIdx,
    "consent must be resolved (success or swallowed failure) before completeOnboarding runs");
  // submitConsent's own contract (proven above) is that it never throws — a
  // rejected saveTelemetryConsent is caught internally and still resolves.
  // So the only thing this call site needs to get right is NOT wrapping the
  // await in a try/catch that could re-throw past it.
  assert.doesNotMatch(
    body.slice(consentIdx - 40, consentIdx),
    /try\s*\{\s*$/,
    "finish() must not re-wrap submitConsent in a try that could re-throw its result",
  );
});

test("completeOnboarding only carries telemetry_asked when the step actually ran", () => {
  assert.match(
    jsx,
    /\.\.\.\(askTelemetry \? \{ telemetry_asked: c\.telemetryAsked \} : \{\}\)/,
    "an already-asked install must not resend telemetry_asked and re-trigger persistence",
  );
});
