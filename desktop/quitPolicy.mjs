// What `before-quit` must do. Extracted because the previous round put this
// orchestration back into main.mjs (which imports electron) and it shipped a
// blocker immediately: the RE-ENTRANT handler did not preventDefault, so a
// second Cmd-Q during the shutdown delay abandoned the escalation and leaked
// the server.
//
//   "delay"     — hold the quit and start shutting the servers down
//   "keep-held" — a shutdown is already running; hold again, do NOT start another
//   "proceed"   — nothing of ours is running, or shutdown finished; let it quit

export function quitAction({ ownsAny, shuttingDown, shutdownDone }) {
  if (shutdownDone) return "proceed";
  if (shuttingDown) return "keep-held";
  return ownsAny ? "delay" : "proceed";
}
