// Incident 2026-08-12 ~02:30: the board's websocket died during a server
// restart and the SPA silently kept rendering its last init snapshot — two
// DONE tasks stayed pinned in "review pr" until the operator hard-reloaded.
// `App.jsx`'s old effect closed over a fixed 3000ms `setTimeout(connect,
// 3000)` and never re-fetched the snapshot on reconnect, so a live socket
// sitting over stale state was invisible.
//
// This module is the whole of the fix, kept dependency-free (no WebSocket or
// timer global referenced directly) so the backoff and re-sync logic can be
// driven from `node --test` with a fake clock and a fake socket. `App.jsx`
// wires it to the real `connectWS`/`fetchTasks`.
//
// `readyState`-shaped phases: "connecting" (never yet open) | "resyncing"
// (open, snapshot not yet landed) | "live" (open + fresh snapshot delivered)
// | "disconnected" (closed/errored, backoff pending) | "sync-failed" (open,
// but the snapshot fetch itself has exhausted its retries — never silently
// given up, per the incident: a stale-but-quiet board is the defect).

export const INITIAL_DELAY_MS = 1000;
export const MAX_DELAY_MS = 30000;
export const SNAPSHOT_RETRIES = 4;
export const SNAPSHOT_INITIAL_MS = 250;

/** Exponential backoff, capped: 1s, 2s, 4s, 8s, 16s, 30s, 30s, 30s… forever. */
export function backoffDelay(attempt) {
  return Math.min(INITIAL_DELAY_MS * 2 ** attempt, MAX_DELAY_MS);
}

/**
 * `deps.connect(handlers)` opens a socket and must call `handlers.onOpen()` /
 * `handlers.onClose()` when it does — a paired error+close from the same
 * socket is expected to reach `onClose` more than once and is deduped here,
 * not by the caller (`api.js` routes both `onerror` and `onclose` to the same
 * `onClose` prop).
 *
 * `deps.fetchSnapshot()` resolves the full board snapshot; `deps.onSnapshot`
 * receives it (the caller dispatches a wholesale replace, never a merge).
 * `deps.onStatus(phase)` fires on every phase transition.
 * `deps.setTimeout`/`deps.clearTimeout` default to the globals so tests can
 * substitute a fake clock.
 */
export function createReconnector({
  connect,
  fetchSnapshot,
  onSnapshot,
  onStatus,
  setTimeout: scheduleTimeout = globalThis.setTimeout,
  clearTimeout: cancelTimeout = globalThis.clearTimeout,
}) {
  let phase = "connecting";
  let attempt = 0;
  let generation = 0;
  let snapshotAttempt = 0;
  let reconnectTimer = null;
  let snapshotTimer = null;
  let socket = null;
  let handledThisConnect = false;
  let stopped = true;

  function setPhase(next) {
    phase = next;
    if (onStatus) onStatus(phase);
  }

  function runSnapshotAttempt(gen) {
    fetchSnapshot().then(
      (snapshot) => {
        if (gen !== generation) return; // superseded by a later connect/disconnect
        if (onSnapshot) onSnapshot(snapshot);
        setPhase("live");
      },
      () => {
        if (gen !== generation) return;
        if (snapshotAttempt < SNAPSHOT_RETRIES) {
          const delay = SNAPSHOT_INITIAL_MS * 2 ** snapshotAttempt;
          snapshotAttempt += 1;
          snapshotTimer = scheduleTimeout(() => runSnapshotAttempt(gen), delay);
        } else {
          setPhase("sync-failed");
          const delay = SNAPSHOT_INITIAL_MS * 2 ** SNAPSHOT_RETRIES;
          snapshotTimer = scheduleTimeout(() => runSnapshotAttempt(gen), delay);
        }
      },
    );
  }

  function startSnapshotFetch() {
    snapshotAttempt = 0;
    if (snapshotTimer) {
      cancelTimeout(snapshotTimer);
      snapshotTimer = null;
    }
    runSnapshotAttempt(generation);
  }

  function scheduleReconnect() {
    const delay = backoffDelay(attempt);
    attempt += 1;
    reconnectTimer = scheduleTimeout(() => {
      reconnectTimer = null;
      doConnect();
    }, delay);
  }

  function disconnect() {
    if (handledThisConnect || stopped) return;
    handledThisConnect = true;
    generation += 1; // drops any in-flight snapshot fetch/timer as stale
    if (snapshotTimer) {
      cancelTimeout(snapshotTimer);
      snapshotTimer = null;
    }
    if (reconnectTimer) {
      cancelTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (phase === "live" || phase === "resyncing") attempt = 0;
    setPhase("disconnected");
    scheduleReconnect();
  }

  function doConnect() {
    if (stopped) return;
    handledThisConnect = false;
    setPhase("connecting");
    socket = connect({
      onOpen: () => {
        if (stopped) return;
        setPhase("resyncing");
        startSnapshotFetch();
      },
      onClose: () => disconnect(),
    });
  }

  return {
    start() {
      if (!stopped) return;
      stopped = false;
      generation += 1;
      attempt = 0;
      doConnect();
    },
    stop() {
      if (stopped) return;
      stopped = true;
      generation += 1;
      if (reconnectTimer) {
        cancelTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      if (snapshotTimer) {
        cancelTimeout(snapshotTimer);
        snapshotTimer = null;
      }
      if (socket && typeof socket.close === "function") socket.close();
      socket = null;
    },
    status() {
      return phase;
    },
  };
}
