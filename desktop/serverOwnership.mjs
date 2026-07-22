// Who owns the nh server process, and what a token change should do about it.
//
// This exists because `serverState` was being mutated from three places with no
// single owner, which produced two blocking defects: a probe-up overwrote a
// "spawned" state with "attached", orphaning the child at quit and making the
// tray lie; and a token save stopped a server without waiting or respawning.
// The decisions live here, pure and unit-tested; main.mjs performs the effects.

/**
 * True when THIS shell started the server and still holds its child handle.
 * `failed` counts: ensureServer deliberately leaves a slow-booting nh running
 * and returns its child, and that process may yet bind the port — it is still
 * ours to stop.
 */
export function ownsChild(state) {
  // "attached" is NEVER ours, even if a child handle is somehow present: an
  // operator-started server must never be a kill target. That guard predates
  // this module (server.test.mjs pins it) and is preserved deliberately.
  return Boolean(state && state.child && state.status !== "attached");
}

/**
 * The state after a successful probe. A "spawned" (or child-bearing) state must
 * SURVIVE: overwriting it with "attached" discards the only legitimate kill
 * target, so the server we started outlives the app and the tray reports it as
 * operator-owned.
 */
export function stateOnProbeUp(prev) {
  if (!ownsChild(prev)) return { status: "attached" };
  // It is answering now, so "failed (spawn-timeout)" is stale — the tray read
  // "server: unreachable" over a live board. Keep the child (ownership is what
  // makes it stoppable), correct only the status.
  return prev.status === "spawned" ? prev : { status: "spawned", child: prev.child };
}

/**
 * What saving a new token must do. A running server read its credential once at
 * bootstrap, so a write alone is inert.
 *   "restart"  — ours: stop it, wait for the port to free, spawn again.
 *   "needs-restart" — someone else's: we must not kill it; say so honestly.
 *   "proceed"  — nothing is up; the normal start path applies.
 */
export function saveAction(state, isUp, ownsAlive) {
  // `ownsAlive` is REQUIRED, deliberately with no default. Defaulting it to
  // ownsChild(state) reproduces the exact semantics that shipped two blockers in
  // a row: a state carrying an EXITED child is still "ours" by ownsChild, so we
  // burned 20s trying to stop a corpse and then blamed the wrong party. Omitting
  // it now yields undefined -> falsy -> we never claim a server we may not own,
  // which is the safe direction.
  if (ownsAlive) return "restart";
  if (isUp) return "needs-restart";
  return "proceed";
}

/**
 * Whether a child handle may still be signalled. A reaped child's PID can be
 * REUSED by the OS, and `process.kill(-pid)` has no identity check — signalling
 * a stale PID group can SIGTERM an unrelated process. node's own
 * `child.kill()` no-ops after exit; the group kill has no such protection, so
 * this gate stands in for it.
 */
export function hasExited(child) {
  // Deliberately NOT child.killed: that only records that a signal was SENT.
  // A process ignoring SIGTERM is still alive and must remain signallable,
  // otherwise it is skipped forever and never escalated to.
  return Boolean(child)
    && ((child.exitCode !== null && child.exitCode !== undefined)
        || (child.signalCode !== null && child.signalCode !== undefined));
}

export function canSignal(child) {
  return Boolean(child) && typeof child.pid === "number" && !hasExited(child);
}

/**
 * Track EVERY server this shell spawned. `serverState` holds only the latest,
 * and overwriting it dropped earlier handles — each extra Retry leaked a live
 * nh that nothing could ever stop. Returns the state unchanged for chaining.
 */
export function rememberChild(set, state) {
  if (ownsChild(state)) set.add(state.child);
  return state;
}

/**
 * Stop every tracked server, emptying the registry. `stop` takes a state so it
 * can reuse stopServer's own ownership check. Returns how many were signalled.
 */
export function drainOwned(set, stop) {
  let stopped = 0;
  const survivors = [];
  for (const child of set) {
    if (hasExited(child)) continue;              // really gone; drop the handle
    // A spawn that never produced a pid can never be signalled; keeping it
    // would make ownsAnything() true forever.
    if (typeof child.pid !== "number") continue;
    if (canSignal(child) && stop({ status: "spawned", child })) stopped += 1;
    // KEEP IT REGARDLESS. stopServer returns true when process.kill did not
    // THROW — which it does not for a live process that merely ignores
    // SIGTERM. Treating that as death dropped the handle and leaked the
    // server. Only hasExited() is proof.
    survivors.push(child);
  }
  set.clear();
  for (const child of survivors) set.add(child);
  return stopped;
}

/** True when this shell has ANY live server — registry or current state. */
export function ownsAnything(set, state) {
  if (ownsChild(state)) return true;
  for (const child of set) if (!hasExited(child)) return true;
  return false;
}
