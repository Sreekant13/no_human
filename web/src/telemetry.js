// Opt-in usage telemetry + masked session replay (PostHog), default OFF.
//
// The privacy contract this file enforces, matching the published policy:
//  - CONSENT FIRST: without `telemetry.enabled` AND a publishable client
//    token in /api/config, initTelemetry returns before `posthog-js` is even
//    IMPORTED (dynamic import) — no consent means the module never loads,
//    no cookies, no recorder, zero bytes to PostHog.
//  - MASKED REPLAY: recordings capture the app's OWN interface only. All
//    inputs are masked, plus `.ph-mask` (text-level) and PostHog's own
//    `.ph-no-capture` (whole-block) on every element that renders operator
//    content: task titles, specs/descriptions, diffs, activity logs, the
//    composer, backlog ticket titles. Never your code.
//  - NO PERSON PROFILES: person_profiles "never" — events are keyed to an
//    anonymous instance, not a person.
import { fetchVersion } from "./api.js";

let posthog = null; // the initialized client, or null when not consented
let started = false;

export function telemetryConsent(cfg) {
  const t = cfg?.telemetry;
  if (!t?.enabled) return null;
  // `posthog_publishable` is the ONLY accepted field: it is named so
  // /api/config's secret-scrubber does not mask it. No *_token/*_key
  // fallbacks — those names ARE scrubbed to a "●●● set" literal by the
  // config echo, so a fallback would init PostHog with the mask string.
  const key = t.posthog_publishable;
  return key ? { key, host: t.posthog_host } : null;
}

// `importer` is injectable so tests can prove "no consent => posthog-js never
// imported" without shipping a module loader mock.
export async function initTelemetry(cfg, { importer } = {}) {
  if (started) return posthog;
  const consent = telemetryConsent(cfg);
  if (!consent) return null; // returns BEFORE any posthog-js import
  started = true;
  try {
    // The literal specifier lets vite split posthog-js into a lazy chunk that
    // is only ever fetched on this consented path.
    const load = importer || (() => import("posthog-js"));
    const mod = await load("posthog-js");
    const client = mod.default ?? mod.posthog ?? mod;
    client.init(consent.key, {
      api_host: consent.host,
      defaults: "2026-05-30",
      session_recording: { maskAllInputs: true, maskTextSelector: ".ph-mask" },
      person_profiles: "never",
    });
    // Stamp every event with the running app version (server's /api/version —
    // the same string `nh --version` prints). Best-effort: unknown stays unknown.
    let version = "unknown";
    try {
      version = (await fetchVersion())?.version || "unknown";
    } catch {
      /* best-effort */
    }
    client.register({ app_version: version });
    posthog = client;
    return posthog;
  } catch {
    return null; // telemetry must never break the board
  }
}

// Screen-view events: lane NAMES only (board/backlog/done/failed/stats/about),
// never content. A no-op until initTelemetry has run with consent.
export function captureScreen(screen) {
  try {
    posthog?.capture("screen_viewed", { screen });
  } catch {
    /* never break navigation */
  }
}

// Test seam: reset module state between node:test cases.
export function _resetForTests() {
  posthog = null;
  started = false;
}
