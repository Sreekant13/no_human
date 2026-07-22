// The server-ownership state machine. Two blocking defects came from these
// transitions being implicit and scattered, so they are pinned here.
import assert from "node:assert/strict";
import test from "node:test";

import {
  canSignal, drainOwned, hasExited, ownsAnything, ownsChild, rememberChild,
  saveAction, stateOnProbeUp,
} from "./serverOwnership.mjs";

const live = (pid) => ({ pid, exitCode: null, signalCode: null, killed: false, kill() {} });
const child = live(1);

test("ownsChild: only a state carrying our child handle", () => {
  assert.equal(ownsChild({ status: "spawned", child }), true);
  // ensureServer returns a child on spawn-timeout and leaves it running.
  assert.equal(ownsChild({ status: "failed", reason: "spawn-timeout", child }), true);
  assert.equal(ownsChild({ status: "attached" }), false);
  assert.equal(ownsChild({ status: "failed", reason: "nh-not-found" }), false);
  assert.equal(ownsChild(null), false);
  assert.equal(ownsChild(undefined), false);
});

test("stateOnProbeUp: a spawned state SURVIVES a probe (keeps the kill target)", () => {
  const spawned = { status: "spawned", child };
  assert.equal(stateOnProbeUp(spawned), spawned,
    "overwriting this orphans the server we started and makes the tray lie");
});

test("stateOnProbeUp: a timed-out child that is now answering becomes spawned", () => {
  const timedOut = { status: "failed", reason: "spawn-timeout", child };
  const next = stateOnProbeUp(timedOut);
  assert.equal(next.status, "spawned",
    "the tray read \"unreachable\" while the board was live");
  assert.equal(next.child, child, "ownership must survive, or it cannot be stopped");
  assert.equal(ownsChild(next), true);
});

test("stateOnProbeUp: anything we do not own becomes attached", () => {
  assert.deepEqual(stateOnProbeUp(null), { status: "attached" });
  assert.deepEqual(stateOnProbeUp({ status: "attached" }), { status: "attached" });
  assert.deepEqual(stateOnProbeUp({ status: "failed", reason: "nh-not-found" }),
                   { status: "attached" });
});

test("saveAction: restart what we own, never kill someone else's", () => {
  // The third argument is the caller's LIVENESS verdict; each call below passes
  // what ownsChild(state) would have said, so these keep their original meaning.
  assert.equal(saveAction({ status: "spawned", child }, true, true), "restart");
  assert.equal(saveAction({ status: "spawned", child }, false, true), "restart");
  // The slow-boot case that previously fell through BOTH branches and silently
  // reported success against a server holding the old token.
  assert.equal(saveAction({ status: "failed", reason: "spawn-timeout", child }, true, true),
               "restart");
  assert.equal(saveAction({ status: "attached" }, true, false), "needs-restart");
  assert.equal(saveAction(null, true, false), "needs-restart",
    "a server we never started is the operator's — warn, do not kill");
  assert.equal(saveAction(null, false, false), "proceed");
  assert.equal(saveAction({ status: "failed", reason: "nh-not-found" }, false, false),
               "proceed");
});

test("rememberChild: every spawned child is retained, not just the latest", () => {
  const set = new Set();
  const a = live(1), b = live(2);
  rememberChild(set, { status: "spawned", child: a });
  rememberChild(set, { status: "failed", reason: "spawn-timeout", child: b });
  rememberChild(set, { status: "attached" });          // not ours
  rememberChild(set, { status: "failed", reason: "nh-not-found" });
  assert.deepEqual([...set], [a, b],
    "overwriting serverState must not lose an earlier child");
});

test("drainOwned: signals every tracked child, not just the last", () => {
  const killed = [];
  const set = new Set([live(1), live(2), live(3)]);
  const stopped = drainOwned(set, (state) => { killed.push(state.child.pid); return true; });
  assert.equal(stopped, 3, "quit must signal ALL of them, not just the last");
  assert.deepEqual(killed, [1, 2, 3]);
  assert.equal(set.size, 3,
    "they are still alive until they exit — dropping them here leaked servers");
});

test("drainOwned: counts only what actually stopped", () => {
  const set = new Set([live(1), live(2)]);
  assert.equal(drainOwned(set, (s) => s.child.pid === 1), 1);
});

test("canSignal: never signal a child that has already exited (PID reuse)", () => {
  const live = { pid: 10, exitCode: null, signalCode: null, killed: false };
  assert.equal(canSignal(live), true);
  assert.equal(canSignal({ ...live, exitCode: 0 }), false, "exited normally");
  assert.equal(canSignal({ ...live, signalCode: "SIGTERM" }), false, "already signalled");
  // `killed` only means a signal was sent; a child ignoring SIGTERM must stay
  // signallable so it can be escalated rather than skipped forever.
  assert.equal(canSignal({ ...live, killed: true }), true);
  assert.equal(canSignal({ ...live, pid: undefined }), false, "spawn failed");
  assert.equal(canSignal(null), false);
});

test("drainOwned: skips dead handles — a reused PID must never be signalled", () => {
  const signalled = [];
  const live = { pid: 1, exitCode: null, signalCode: null, killed: false };
  const dead = { pid: 2, exitCode: 0, signalCode: null, killed: false };
  const set = new Set([live, dead]);
  const stopped = drainOwned(set, (s) => { signalled.push(s.child.pid); return true; });
  assert.deepEqual(signalled, [1], "signalling pid 2 could hit an unrelated group");
  assert.equal(stopped, 1);
  assert.deepEqual([...set], [live], "the exited one is dropped, the live one kept");
});

test("hasExited: distinguishes a finished process from a handle with no pid", () => {
  assert.equal(hasExited({ pid: 1, exitCode: 0, signalCode: null }), true);
  assert.equal(hasExited({ pid: 1, exitCode: null, signalCode: "SIGTERM" }), true);
  assert.equal(hasExited({ pid: 1, exitCode: null, signalCode: null, killed: true }),
    false, "signalled is not the same as exited");
  assert.equal(hasExited({ pid: 1, exitCode: null, signalCode: null, killed: false }), false);
  // A bare test double (no lifecycle fields) is NOT "exited" — it is just a stub.
  assert.equal(hasExited({ kill() {} }), false);
  assert.equal(hasExited(null), false);
});

test("drainOwned: a signalled-but-still-alive child STAYS tracked", () => {
  // The real stopServer returns TRUE whenever process.kill did not throw, which
  // is what happens for a live process that ignores SIGTERM. Modelling that as
  // `false` tested a case that cannot occur while the real one went unguarded.
  const stubborn = live(7);
  const ok = live(8);
  const set = new Set([stubborn, ok]);
  const stopped = drainOwned(set, () => true);   // "succeeded", nothing died
  assert.equal(stopped, 2);
  assert.deepEqual([...set], [stubborn, ok],
    "a successful signal is not proof of death — only hasExited() is");
  // Once they really exit they are dropped.
  stubborn.exitCode = 0; ok.signalCode = "SIGTERM";
  assert.equal(drainOwned(set, () => true), 0);
  assert.equal(set.size, 0);
});

test("drainOwned: a spawn that produced no pid is discarded, not kept forever", () => {
  const set = new Set([{ pid: undefined, exitCode: null, signalCode: null }]);
  assert.equal(drainOwned(set, () => true), 0);
  assert.equal(set.size, 0, "an unsignalable handle kept ownsAnything() true forever");
});

test("drainOwned: an exited child is dropped, not retried", () => {
  const set = new Set([{ pid: 9, exitCode: 0, signalCode: null }]);
  assert.equal(drainOwned(set, () => true), 0);
  assert.equal(set.size, 0);
});

test("ownsAnything: sees a boot still in flight, not just the published state", () => {
  const set = new Set();
  assert.equal(ownsAnything(set, null), false);
  set.add(live(11));
  assert.equal(ownsAnything(set, null), true,
    "a server spawned but not yet published must not look like 'nothing running'");
  const dead = new Set([{ pid: 12, exitCode: 0, signalCode: null }]);
  assert.equal(ownsAnything(dead, null), false);
  assert.equal(ownsAnything(new Set(), { status: "spawned", child: live(13) }), true);
});

test("saveAction: the caller's liveness verdict overrides bare ownership", () => {
  const dead = { pid: 9, exitCode: 0, signalCode: null };
  const state = { status: "spawned", child: dead };
  // Omitting the verdict must FAIL SAFE. Defaulting it to ownsChild(state) — as
  // it did — says "restart" for a corpse: 20s spent stopping nothing, then the
  // wrong party blamed.
  assert.equal(saveAction(state, true), "needs-restart",
    "a caller that forgets the liveness verdict must not claim the server");
  assert.equal(saveAction(state, true, false), "needs-restart",
    "with our child dead and the port answering, it is somebody else's server");
  assert.equal(saveAction(null, false, true), "restart",
    "mid-boot there is no state yet, but the boot is still ours to restart");
});
