// Owning, stopping and restarting the servers this shell spawned.
//
// This lived inline in main.mjs, which imports electron. I claimed that made it
// untestable; a reviewer disproved that, and both of the last two blocking
// defects lived in exactly this code. Dependencies are injected so it runs
// under `node --test` with no electron at all.

import {
  drainOwned, hasExited, ownsAnything, ownsChild, rememberChild,
} from "./serverOwnership.mjs";

/**
 * @param probe        () => Promise<"up"|"down">
 * @param stopServer   (state) => boolean
 * @param sleep        (ms) => Promise
 */
export function createServerLifecycle({ probe, stopServer, forceStopServer,
                                       sleep }) {
  const owned = new Set();
  let state = null;

  return {
    get state() { return state; },
    set state(next) { state = next; },
    get owned() { return owned; },

    /**
     * Track a child the moment it exists. The `exit` listener evicts it when it
     * dies; a spawn that FAILED (ENOENT/EACCES) never emits `exit`, but node
     * sets exitCode so hasExited() is true and drainOwned discards it.
     */
    track(child) {
      rememberChild(owned, { status: "spawned", child });
      if (child && typeof child.once === "function") {
        child.once("exit", () => owned.delete(child));
      }
      return child;
    },

    /** Do we have ANY server of ours — including one still booting? */
    ownsAny() { return ownsAnything(owned, state); },

    /**
     * Do we have any server of ours that is still ALIVE?
     *
     * ownsAny() alone is not that: ownsChild() never checks liveness, and the
     * exit listener evicts a dead child from `owned` but never clears `state`,
     * so a corpse sits in `state` forever. Two rounds shipped opposite halves of
     * this — `state`-only was wrong mid-boot (child tracked, state still null),
     * `ownsAny` was wrong once our child died while a FOREIGN server held the
     * port. This is the predicate both of them were reaching for.
     */
    ownsAnyAlive() {
      for (const child of owned) if (!hasExited(child)) return true;
      return ownsChild(state) && !hasExited(state.child);
    },

    /** Stop every server we own. Handles that survive stay tracked. */
    stopAll() { return drainOwned(owned, stopServer); },

    /**
     * Shut down for quit: SIGTERM, a short grace period, then SIGKILL anything
     * still alive. Without the escalation a server that traps SIGTERM outlives
     * the app and keeps the port. Returns when everything of ours has exited or
     * the deadline passes — the caller must quit regardless.
     */
    async shutdown(graceMs = 10000, intervalMs = 100) {
      const targets = [...owned];
      if (ownsChild(state) && !targets.includes(state.child)) targets.push(state.child);
      if (targets.length === 0) return true;

      this.stopAll();
      stopServer(state);
      const deadline = Date.now() + graceMs;
      while (Date.now() < deadline) {
        if (targets.every(hasExited)) return true;
        await sleep(intervalMs);
      }
      for (const child of targets) {
        if (!hasExited(child)) forceStopServer({ status: "spawned", child });
      }
      return targets.every(hasExited);
    },

    /**
     * Stop the server we own and WAIT for the port to free. Returns false when
     * it never goes down — the caller must NOT then fall through to starting
     * another, or it attaches to the old one and loses the handle.
     */
     async restart(timeoutMs = 20000, intervalMs = 250, graceMs = 5000) {
      // Everything we might have to kill, captured before we start signalling.
      const targets = [...owned];
      if (ownsChild(state) && !targets.includes(state.child)) targets.push(state.child);

      this.stopAll();
      stopServer(state);

      const start = Date.now();
      let escalated = false;
      while (Date.now() - start < timeoutMs) {
        // Wait for the CHILD to exit, not merely for the port to go quiet:
        // mid-boot the port was never bound, so a port check returned "down"
        // instantly while the old server was still about to bind it.
        if (targets.every(hasExited) && (await probe()) === "down") {
          state = null;
          return true;
        }
        if (!escalated && Date.now() - start >= graceMs) {
          // SIGTERM was ignored. Escalate, or it survives quit holding the port.
          escalated = true;
          for (const child of targets) {
            if (!hasExited(child)) forceStopServer({ status: "spawned", child });
          }
        }
        await sleep(intervalMs);
      }
      return false;
    },

    /** True when the CURRENT state is a server of ours (not an attached one). */
    ownsCurrent() { return ownsChild(state); },

    /**
     * The state to publish when we could not stop our own server. It MUST keep
     * the child: dropping it made the next navigation see "not ours", skip the
     * restart guard, and attach to the very server we failed to stop — the
     * board then ran on the OLD token with the handle gone for good.
     */
    failedStopState() {
      return { ...(state ?? {}), status: "failed", reason: "stop-failed" };
    },

    /**
     * True while we hold a server we FAILED to stop and which is still alive.
     * A probe-up in this state is almost certainly that very server — still on
     * the OLD token — so attaching to it would put the board on stale
     * credentials and let the tray report it as one we spawned. Once the child
     * genuinely exits this goes false and normal attach resumes.
     */
    heldStopFailed() {
      return Boolean(state && state.reason === "stop-failed")
        && this.currentChildAlive();
    },

    /**
     * True when the server we currently hold is still running. This is the
     * discriminator for "whose server is on the port": if our child is alive it
     * is ours, and if it has exited while the port still answers, the server
     * belongs to someone else (an `nh start` in a terminal).
     */
    currentChildAlive() {
      return Boolean(state && ownsChild(state) && !hasExited(state.child));
    },
  };
}
