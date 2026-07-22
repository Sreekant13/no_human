// Serializes window navigations and lets a newer intent supersede older ones.
//
// This lived inline in main.mjs, which imports electron and so is untestable —
// and it went wrong twice. Coalescing made a token save report success off a
// navigation started for the OLD token; then the supersede check claimed its
// generation when a navigation STARTED, so one already queued re-took ownership
// after the user opened the credential screen and painted over it.

/**
 * @param run  (arg, isCurrent) => Promise — performs one navigation. It must
 *             call isCurrent() before painting or publishing state; false means
 *             something newer owns the window.
 * @param timeoutMs  a hung navigation must not block every later one forever.
 */
export function createNavScheduler(run, timeoutMs = 90000) {
  let chain = Promise.resolve();
  let generation = 0;
  let pending = 0;

  function withTimeout(promise, onTimeout) {
    if (!timeoutMs) return promise;
    let timer;
    return Promise.race([
      promise.finally(() => clearTimeout(timer)),
      new Promise((_r, rej) => { timer = setTimeout(() => {
        onTimeout?.();
        rej(new Error("navigation timed out"));
      }, timeoutMs); }),
    ]);
  }

  return {
    /**
     * Queue a navigation. The generation is claimed AT ENQUEUE, so a later
     * supersede() invalidates navigations that are queued as well as running.
     */
    schedule(arg) {
      const mine = ++generation;
      pending += 1;
      const next = chain
        .catch(() => {})
        .then(() => withTimeout(run(arg, () => mine === generation), () => {
          // Timed out: we have given up on this nav, so stand it down — its
          // isCurrent() now returns false and it can no longer paint or spawn.
          // Counting it as pending forever would leave Retry permanently dead.
          if (mine === generation) generation += 1;
        }));
      chain = next.catch(() => {});          // a failure must not wedge the queue
      return next.finally(() => { pending -= 1; });
    },

    /**
     * Schedule ONLY if nothing is in flight. Retry is a plain link the user can
     * click repeatedly, and each accepted click is a full ensureServer plus
     * another spawned server. Returns null when it declines, so the caller can
     * tell "ran" from "ignored".
     *
     * Deliberately NOT used for saving a token or dismissing: those must always
     * get their own run.
     */
    scheduleIfIdle(arg) {
      if (pending > 0) return null;
      return this.schedule(arg);
    },

    /** Something else now owns the window; every queued/running nav stands down. */
    supersede() { generation += 1; },

    /** How many navigations are queued or running (0 when idle). */
    get pending() { return pending; },
  };
}
