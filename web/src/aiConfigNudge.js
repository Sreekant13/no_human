// The Settings "!" nudge: onboarding no longer walks the AI-learnings steps
// (they left the wizard 2026-08-30), so a one-time nudge points the user at
// Settings to finish their AI configuration — review rules, pick models, seed
// the second brain. A "!" badge sits on the Settings nav row with the tooltip
// "Complete AI configuration", and a small popup prompts once after onboarding.
//
// It is a per-install, per-browser acknowledgement, so localStorage is the
// right home (like the theme toggle next to it) — not server state. Cleared the
// first time the user opens Settings, so it nudges once and never nags. Every
// access is guarded: localStorage throws in private windows / blocked-storage
// browsers, and a throw there must degrade to "show the nudge", never crash the
// board.

const KEY = "nh-ai-config-done";

/** True once the user has acknowledged AI setup (opened Settings, or dismissed
 *  the popup). Fail-open to NOT done (show the nudge) if storage is unreadable —
 *  a returning user seeing the nudge once more is harmless; a crashed board is
 *  not. */
export function isAiConfigDone(storage = safeStorage()) {
  try {
    return storage?.getItem(KEY) === "1";
  } catch {
    return false;
  }
}

/** Record the acknowledgement. A write failure is swallowed: the nudge simply
 *  shows again next load, which is strictly better than throwing into a click
 *  handler. */
export function markAiConfigDone(storage = safeStorage()) {
  try {
    storage?.setItem(KEY, "1");
  } catch {
    /* storage blocked — the nudge will reappear, which is acceptable */
  }
}

function safeStorage() {
  try {
    return typeof localStorage !== "undefined" ? localStorage : null;
  } catch {
    return null; // accessing the property itself can throw (sandboxed frames)
  }
}
