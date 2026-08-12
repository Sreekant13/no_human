// Incident 2026-08-12 ~02:30: the board's websocket died during a server
// restart, the SPA kept rendering its last init snapshot, and two DONE tasks
// (03a2df4a, 6dc941c6) stayed pinned in "review pr" until a hard reload. The
// old effect (App.jsx, pre-fix) closed over a fixed `setTimeout(connect,
// 3000)` and never re-fetched the snapshot on reconnect. These tests drive
// `createReconnector` with a fake clock and a fake socket so the backoff
// schedule and the "never merge stale state" guarantee are assertable
// without a browser.
import test from "node:test";
import assert from "node:assert/strict";
import { createReconnector, backoffDelay, INITIAL_DELAY_MS, MAX_DELAY_MS, SNAPSHOT_RETRIES, SNAPSHOT_INITIAL_MS } from "./wsReconnect.js";
import { tasksReducer } from "./tasksReducer.js";

/** A queue of {at, fn} timers a test advances by hand — no real clock. */
function fakeClock() {
  let now = 0;
  let nextId = 1;
  const pending = new Map();
  return {
    setTimeout(fn, ms) {
      const id = nextId++;
      pending.set(id, { at: now + ms, fn, ms });
      return id;
    },
    clearTimeout(id) {
      pending.delete(id);
    },
    /** Fire every timer due at or before `now + ms`, in schedule order. */
    advance(ms) {
      now += ms;
      const due = [...pending.entries()]
        .filter(([, t]) => t.at <= now)
        .sort((a, b) => a[1].at - b[1].at);
      for (const [id, t] of due) {
        pending.delete(id);
        t.fn();
      }
    },
    pendingCount() {
      return pending.size;
    },
    pendingDelays() {
      return [...pending.values()].map((t) => t.ms);
    },
  };
}

/** A fake `connect` that records every construction and hands back a fake socket. */
function fakeConnector() {
  const sockets = [];
  let lastHandlers = null;
  return {
    connect(handlers) {
      lastHandlers = handlers;
      const socket = { closed: false, close: () => { socket.closed = true; } };
      sockets.push(socket);
      return socket;
    },
    sockets,
    open() { lastHandlers.onOpen(); },
    close() { lastHandlers.onClose(); },
    error() { lastHandlers.onClose(); }, // api.js routes onerror to the same onClose prop
  };
}

function pendingSnapshot() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

test("backoffDelay: 1s, 2s, 4s, 8s, 16s, then capped at 30s forever", () => {
  assert.deepEqual(
    [0, 1, 2, 3, 4, 5, 6, 19].map(backoffDelay),
    [1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000],
  );
  assert.equal(backoffDelay(0), INITIAL_DELAY_MS);
  assert.ok(backoffDelay(50) <= MAX_DELAY_MS);
});

test("reconnect delays are 1s, 2s, 4s, 8s, 16s, then capped at 30s forever", () => {
  const clock = fakeClock();
  const fc = fakeConnector();
  const delays = [];
  const r = createReconnector({
    connect: fc.connect,
    fetchSnapshot: () => new Promise(() => {}), // never resolves — irrelevant here
    onSnapshot: () => {},
    onStatus: () => {},
    setTimeout: (fn, ms) => { delays.push(ms); return clock.setTimeout(fn, ms); },
    clearTimeout: clock.clearTimeout,
  });
  r.start();
  for (let i = 0; i < 6; i++) {
    fc.close(); // close-without-open: the socket never reached "open"
    clock.advance(60000); // fire the scheduled reconnect, well past any cap
  }
  assert.deepEqual(delays, [1000, 2000, 4000, 8000, 16000, 30000]);
  // 7th and 20th failure are both still capped at 30000, not growing or stopping.
  for (let i = 0; i < 14; i++) { fc.close(); clock.advance(60000); }
  assert.equal(delays.length, 20);
  assert.equal(delays[6], 30000);
  assert.equal(delays[19], 30000);
  r.stop();
});

test("socket.onerror triggers a reconnect", () => {
  const clock = fakeClock();
  const fc = fakeConnector();
  const r = createReconnector({
    connect: fc.connect,
    fetchSnapshot: () => new Promise(() => {}),
    onSnapshot: () => {},
    onStatus: () => {},
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });
  r.start();
  assert.equal(fc.sockets.length, 1);
  fc.error();
  clock.advance(INITIAL_DELAY_MS);
  assert.equal(fc.sockets.length, 2, "onerror alone must schedule a reconnect");
  r.stop();
});

test("a paired error+close counts as ONE disconnect, not two", () => {
  const clock = fakeClock();
  const fc = fakeConnector();
  const r = createReconnector({
    connect: fc.connect,
    fetchSnapshot: () => new Promise(() => {}),
    onSnapshot: () => {},
    onStatus: () => {},
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });
  r.start();
  fc.error();
  fc.close(); // the same underlying failure, reported twice
  clock.advance(60000);
  assert.equal(fc.sockets.length, 2, "exactly one reconnect for the paired event, not two");
  r.stop();
});

test("the reconnector never stops retrying", () => {
  const clock = fakeClock();
  const fc = fakeConnector();
  const r = createReconnector({
    connect: fc.connect,
    fetchSnapshot: () => new Promise(() => {}),
    onSnapshot: () => {},
    onStatus: () => {},
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });
  r.start();
  for (let i = 0; i < 50; i++) { fc.close(); clock.advance(60000); }
  assert.equal(fc.sockets.length, 51, "50 failures + the initial connect must still be followed by a 51st attempt");
  fc.close();
  clock.advance(60000);
  assert.equal(fc.sockets.length, 52);
  r.stop();
});

test("on open, the init snapshot is re-fetched and delivered", async () => {
  const clock = fakeClock();
  const fc = fakeConnector();
  let fetchCount = 0;
  let snapshots = [];
  const snaps = [["A"], ["B"]];
  const r = createReconnector({
    connect: fc.connect,
    fetchSnapshot: () => { const s = snaps[fetchCount]; fetchCount += 1; return Promise.resolve(s); },
    onSnapshot: (s) => snapshots.push(s),
    onStatus: () => {},
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });
  r.start();
  fc.open();
  await Promise.resolve(); await Promise.resolve();
  assert.equal(fetchCount, 1);
  assert.deepEqual(snapshots, [["A"]]);
  assert.equal(r.status(), "live");

  fc.close();
  clock.advance(INITIAL_DELAY_MS);
  fc.open();
  await Promise.resolve(); await Promise.resolve();
  assert.equal(fetchCount, 2, "reconnect must re-request the snapshot, not rely on a broadcast");
  assert.deepEqual(snapshots, [["A"], ["B"]]);
  r.stop();
});

test("onSnapshot delivers the fresh snapshot verbatim — the stale array is not merged into", async () => {
  const clock = fakeClock();
  const fc = fakeConnector();
  const A = { id: "03a2df4a", lane: "review pr" };
  const B = { id: "6dc941c6", lane: "review pr" };
  const first = [A, B];
  const second = [{ id: "03a2df4a", lane: "done" }];
  let call = 0;
  let delivered = null;
  const r = createReconnector({
    connect: fc.connect,
    fetchSnapshot: () => { const s = call === 0 ? first : second; call += 1; return Promise.resolve(s); },
    onSnapshot: (s) => { delivered = s; },
    onStatus: () => {},
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });
  r.start();
  fc.open();
  await Promise.resolve(); await Promise.resolve();
  assert.equal(delivered, first);

  fc.close();
  clock.advance(INITIAL_DELAY_MS);
  fc.open();
  await Promise.resolve(); await Promise.resolve();

  assert.equal(delivered, second, "delivered snapshot must be the exact new array, strictly ===");
  assert.equal(delivered.length, 1);

  // Replay the incident through the real reducer: no trace of the stale "review pr" lane.
  const stale = tasksReducer([], { type: "set", tasks: first });
  const resynced = tasksReducer(stale, { type: "set", tasks: second });
  assert.equal(resynced, second);
  assert.equal(resynced.length, 1);
  assert.equal(resynced.find((t) => t.id === "03a2df4a").lane, "done");
  r.stop();
});

test("a failing snapshot fetch retries on a shorter backoff and never publishes 'live'", async () => {
  const clock = fakeClock();
  const fc = fakeConnector();
  const delays = [];
  const statuses = [];
  const r = createReconnector({
    connect: fc.connect,
    fetchSnapshot: () => Promise.reject(new Error("network error")),
    onSnapshot: () => {},
    onStatus: (p) => statuses.push(p),
    setTimeout: (fn, ms) => { delays.push(ms); return clock.setTimeout(fn, ms); },
    clearTimeout: clock.clearTimeout,
  });
  r.start();
  fc.open();
  await Promise.resolve(); await Promise.resolve();
  for (let i = 0; i < SNAPSHOT_RETRIES; i++) {
    clock.advance(SNAPSHOT_INITIAL_MS * 2 ** i);
    await Promise.resolve(); await Promise.resolve();
  }
  // No socket close happens in this test, so every scheduled timer belongs to
  // the snapshot retry ladder: 250, 500, 1000, 2000, then the fixed post-
  // exhaustion interval (4000) that keeps retrying forever.
  assert.deepEqual(delays.slice(0, 4), [250, 500, 1000, 2000]);
  assert.equal(r.status(), "sync-failed");
  assert.ok(!statuses.includes("live"), "a failed sync must never publish 'live'");
  r.stop();
});

test("a close during an in-flight snapshot cancels it and restarts backoff at 1s", async () => {
  const clock = fakeClock();
  const fc = fakeConnector();
  const pending1 = pendingSnapshot();
  const pending2 = pendingSnapshot();
  let call = 0;
  let delivered = [];
  const delays = [];
  const r = createReconnector({
    connect: fc.connect,
    fetchSnapshot: () => { call += 1; return call === 1 ? pending1.promise : pending2.promise; },
    onSnapshot: (s) => delivered.push(s),
    onStatus: () => {},
    setTimeout: (fn, ms) => { delays.push(ms); return clock.setTimeout(fn, ms); },
    clearTimeout: clock.clearTimeout,
  });
  r.start();
  fc.open();
  await Promise.resolve();
  assert.equal(call, 1, "the first snapshot fetch is in flight");

  fc.close(); // drop while the fetch is still pending
  // The stale fetch resolves AFTER the close.
  pending1.resolve(["stale"]);
  await Promise.resolve(); await Promise.resolve();
  assert.deepEqual(delivered, [], "a snapshot from a superseded connection must never be delivered");

  assert.equal(delays[delays.length - 1], INITIAL_DELAY_MS, "backoff restarts at 1s after the drop");
  r.stop();
});

test("stop() is idempotent and leaves no pending timer or open socket", () => {
  const clock = fakeClock();
  const fc = fakeConnector();
  const r = createReconnector({
    connect: fc.connect,
    fetchSnapshot: () => new Promise(() => {}),
    onSnapshot: () => {},
    onStatus: () => {},
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });
  r.start();
  fc.close();
  assert.equal(clock.pendingCount(), 1);
  r.stop();
  assert.equal(clock.pendingCount(), 0);
  assert.ok(fc.sockets[0].closed);
  const socketCountAfterFirstStop = fc.sockets.length;
  r.stop(); // idempotent — must not throw or open another socket
  assert.equal(fc.sockets.length, socketCountAfterFirstStop);
  clock.advance(60000);
  assert.equal(fc.sockets.length, socketCountAfterFirstStop, "no reconnect after stop()");
});
