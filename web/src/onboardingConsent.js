// Telemetry consent helpers. The onboarding consent STEP and the Settings >
// Usage insights pane that once showed the question were removed (operator,
// 2026-08-26): telemetry ships on, with config.yaml `telemetry.enabled: false`
// the one opt-out, and this module is no longer rendered by Onboarding.jsx. The
// QUESTION constant is kept as the canonical privacy-posture wording — a
// byte-identical twin of no_human.config's TELEMETRY_CONSENT_QUESTION, pinned
// together by tests/test_telemetry.py so a comment edit and a copy edit cannot
// part ways.
export const TELEMETRY_CONSENT_QUESTION =
  "Share anonymous usage events and masked screen recordings of the app's own interface — never code, prompts, titles, paths or tokens?";
export const CONSENT_YES_LABEL = "Yes, share";
export const CONSENT_NO_LABEL = "No";

/** The step renders only while the install has never been asked. */
export function shouldAskTelemetry(status) {
  return !(status && status.telemetry_asked);
}

/** yes | no | not-shown → what the launch must do. `answer` is `true`, `false`,
 *  or null/undefined (the step was not shown — an install already asked before).
 *  Usage insights now default ON (opt-out), so BOTH a Yes and an explicit No are
 *  persisted: the server default is enabled, and staying silent on a No would
 *  leave telemetry on. Only the not-shown case writes nothing. */
export async function submitConsent(answer, { saveTelemetryConsent, onError } = {}) {
  if (answer === null || answer === undefined)
    return { enabled: false, called: false, telemetryAsked: true };
  try {
    await saveTelemetryConsent(answer === true);
    return { enabled: answer === true, called: true, telemetryAsked: true };
  } catch (err) {
    if (onError) onError(err);
    // A telemetry failure must never block the launch (AC5) — but it must
    // ALSO never be recorded as "asked". If it were, shouldAskTelemetry()
    // would return false forever: the user made a choice, the write failed (a
    // possibly-transient fault), and they would never get another chance to
    // set it. Leaving telemetryAsked false here means onboarding_complete's
    // sticky patch (app.py) omits the key entirely, so the next launch asks
    // again instead of silently losing the choice.
    return { enabled: false, called: true, error: true, telemetryAsked: false };
  }
}
