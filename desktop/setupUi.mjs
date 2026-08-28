// Decisions for the credential screen, split out so they are unit-testable.
//
// These encode a regression that shipped once: the screen was written for first
// run, where quitting is the only sensible secondary action. Once File >
// Re-enter Claude Token made it reachable OVER a live board, that same action
// quit the app and stopped a shell-spawned server on one keystroke.

/** Whether a board is reachable behind the screen, from the loader's query. */
export function parseCanReturn(search) {
  return new URLSearchParams(search || "").get("canReturn") === "1";
}

/**
 * What the secondary control (and Escape) must do. NEVER "quit" when a board is
 * behind the screen — that is the regression above.
 */
export function dismissTarget(canReturn) {
  return canReturn ? "dismiss" : "quit";
}

/** Copy that has to change with context, so the buttons never lie. `mode` is
 *  the credential type the operator has selected on the screen:
 *  "subscription" (default) or "api_key" (the sanctioned BYO-key opt-in). */
export function labels(canReturn, mode = "subscription") {
  if (canReturn) {
    return {
      primary: "Save and restart",
      secondary: "Back to board",
      hint: "Saving restarts the no_human server so it picks up the new " +
            "credential — a task running right now will be interrupted.",
    };
  }
  return {
    primary: "Save and start",
    secondary: "Quit",
    hint: mode === "api_key"
      ? "The key is stored only on this computer, in ~/.no_human/.env, and " +
        "bills your Anthropic account directly."
      : "Use the token from `claude setup-token`. Have your own Anthropic " +
        "API key instead? Switch the credential type above.",
  };
}

/**
 * The OPTIONAL codex OpenAI key to hand the main process, given the selected
 * codex mode and the field's contents. Returns "" — meaning "write nothing for
 * OpenAI" — for EVERY path except an explicit api_key choice with a non-empty
 * field. So a skipped section (no codex radio chosen) and codex SUBSCRIPTION
 * both send nothing, which is the whole of constraint #6b at the UI layer: in
 * subscription mode no_human stores no OpenAI credential. The Claude credential
 * is unaffected — it is sent separately and stays required.
 */
export function codexKeyToSend(codexMode, fieldValue) {
  return codexMode === "api_key" ? (fieldValue || "").trim() : "";
}

/**
 * What to say while a save is in flight, by elapsed time.
 *
 * The save path can legitimately take ~43s: stopping the old server is capped
 * at 20s, then two 1.5s probes, then up to 20s waiting for the replacement to
 * bind. Previously the screen showed ONE static string for all of it with both
 * buttons disabled — indistinguishable from a hang, and the user's only move
 * was to force-quit. The thresholds are deliberately few: #msg is role="alert",
 * so every change is announced assertively and chatty updates would be worse
 * than none.
 */
export function saveProgress(elapsedMs, canReturn = false) {
  if (elapsedMs < 3000) return "Saving and starting the server…";
  if (!canReturn) {
    // FIRST RUN: there is no old server. Saying "stopping the old server" here
    // is simply false, and this window is easy to reach (a 1.5s probe plus a
    // login-shell lookup plus the spawn wait).
    if (elapsedMs < 25000) return "Starting the server…";
    return "Still working — a first run can take up to a minute.";
  }
  if (elapsedMs < 12000) return "Saved. Stopping the old server…";
  if (elapsedMs < 25000) return "Still working — waiting for the old server to stop…";
  return "Still working — this can take up to a minute.";
}

/**
 * What to say when we saved a token but could not stop the server holding the
 * old one. `weOwnIt` MUST come from the same source that selected the restart
 * branch (ownsAny), not from `state` alone: mid-boot the child is in the owned
 * registry while state is still null, and branching on state told a friend on a
 * packaged DMG to "restart the nh start you ran" when there is no terminal and
 * the culprit is our own child.
 */
export function restartFailedMessage(origin, weOwnIt) {
  return weOwnIt
    ? "Saved, but the server this app started would not stop, so it still holds "
      + "the old token. Quit no_human and open it again."
    : `Saved, but another server is already serving ${origin} and still holds the `
      + "old token. Restart that server to pick up the new one.";
}
