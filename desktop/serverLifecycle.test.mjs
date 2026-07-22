// The orchestration that produced the last three rounds of blocking defects.
//
// The doubles here model the REAL stopServer contract: it returns `true` when
// the signal was DELIVERED, not when the process died. Modelling a stubborn
// server as `stop -> false` tested a case that cannot happen and left the case
// that does happen unguarded.
import assert from "node:assert/strict";
import test from "node:test";

import { createServerLifecycle } from "./serverLifecycle.mjs";
import { ownsChild } from "./serverOwnership.mjs";

const nap = (ms = 0) => new Promise((r) => setTimeout(r, ms));

/** A child that dies only on the signal it is willing to obey. */
function fakeChild(pid, { diesOn = "SIGTERM" } = {}) {
  // once() RECORDS the handler. A no-op double left track()'s exit listener
  // untested, so removing it entirely kept the suite green while the registry
  // grew without bound.
  const handlers = {};
  return { pid, exitCode: null, signalCode: null, killed: false, diesOn,
           handlers,
           once(evt, fn) { handlers[evt] = fn; },
           emit(evt) { handlers[evt]?.(); },
           kill() {} };
}
/**
 * Delivers the signal and returns true — like the real stopServer, which
 * reports DELIVERY, not death. Death is asynchronous: the handle only reports
 * exited on a later turn, which is what the real process does.
 */
const deliver = (signal) => (state) => {
  const c = state.child;
  if (c.diesOn === signal) setTimeout(() => { c.signalCode = signal; }, 0);
  return true;                                      // delivered either way
};

function lifecycleFor(children, { probe = async () => "down" } = {}) {
  const lc = createServerLifecycle({
    probe,
    stopServer: deliver("SIGTERM"),
    forceStopServer: deliver("SIGKILL"),
    sleep: nap,
  });
  children.forEach((c) => lc.track(c));
  return lc;
}

test("track: a boot in flight is owned immediately, before any state is published", () => {
  const lc = lifecycleFor([]);
  assert.equal(lc.ownsAny(), false);
  lc.track(fakeChild(1));
  assert.equal(lc.ownsAny(), true,
    "mid-boot this looked like 'nothing running', so a token save said Connected");
  assert.equal(lc.state, null);
});

test("restart: a SIGTERM-ignoring server is escalated to SIGKILL, not abandoned", async () => {
  const stubborn = fakeChild(2, { diesOn: "SIGKILL" });
  const lc = lifecycleFor([stubborn]);
  lc.state = { status: "spawned", child: stubborn };
  assert.equal(await lc.restart(5000, 1, 5), true,
    "SIGTERM was delivered but ignored; without escalation this never returns");
  assert.equal(stubborn.signalCode, "SIGKILL");
  assert.equal(lc.state, null);
});

test("restart: does NOT report success while our child is still alive", async () => {
  // The port is free the whole time (mid-boot it was never bound) — that must
  // not be mistaken for the old server being gone.
  const immortal = fakeChild(3, { diesOn: "never" });
  const lc = lifecycleFor([immortal], { probe: async () => "down" });
  lc.state = { status: "spawned", child: immortal };
  assert.equal(await lc.restart(30, 1, 5), false,
    "'port is down' is not 'our process exited'");
  assert.deepEqual([...lc.owned], [immortal], "and it must remain killable");
});

test("stopAll: keeps signalled children until they are OBSERVED to have exited", async () => {
  const stubborn = fakeChild(4, { diesOn: "SIGKILL" });   // survives SIGTERM
  const compliant = fakeChild(5);                          // dies on SIGTERM
  const lc = lifecycleFor([stubborn, compliant]);
  assert.equal(lc.stopAll(), 2, "both signals are delivered");
  // Death is asynchronous: at this instant neither is known to be gone, and
  // assuming otherwise is exactly what leaked servers.
  assert.deepEqual([...lc.owned], [stubborn, compliant]);
  await nap(5);                              // the compliant one actually dies
  // A later drain drops whoever exited and re-signals the survivor.
  assert.equal(lc.stopAll(), 1);
  assert.deepEqual([...lc.owned], [stubborn], "still killable at quit");
});

test("ownsCurrent: an attached server is never ours to stop", () => {
  const lc = lifecycleFor([]);
  lc.state = { status: "attached" };
  assert.equal(lc.ownsCurrent(), false);
  lc.state = { status: "spawned", child: fakeChild(6) };
  assert.equal(lc.ownsCurrent(), true);
});

test("shutdown: escalates to SIGKILL so a SIGTERM-trapping server cannot outlive quit", async () => {
  const stubborn = fakeChild(7, { diesOn: "SIGKILL" });
  const lc = lifecycleFor([stubborn]);
  lc.state = { status: "spawned", child: stubborn };
  // The return value reports whether death was OBSERVED before the deadline; a
  // real SIGKILL lands asynchronously, so what must be guaranteed is that the
  // escalation HAPPENED — quit proceeds either way.
  await lc.shutdown(5, 1);
  await nap(5);
  assert.equal(stubborn.signalCode, "SIGKILL",
    "SIGTERM alone left it running after the app exited, still holding the port");
});

test("shutdown: returns immediately when we own nothing", async () => {
  const lc = lifecycleFor([]);
  assert.equal(await lc.shutdown(5000, 1000), true, "quit must not be delayed");
});

test("shutdown: a compliant server exits on SIGTERM without escalation", async () => {
  const good = fakeChild(8);                 // dies on SIGTERM
  const lc = lifecycleFor([good]);
  lc.state = { status: "spawned", child: good };
  assert.equal(await lc.shutdown(5000, 1), true);
  assert.equal(good.signalCode, "SIGTERM", "must not be SIGKILLed needlessly");
});

test("failedStopState: keeps the child, so the next nav still sees it as OURS", () => {
  const child = fakeChild(9);
  const lc = lifecycleFor([child]);
  lc.state = { status: "spawned", child };
  const next = lc.failedStopState();
  assert.equal(next.reason, "stop-failed");
  assert.equal(next.child, child,
    "dropping it made the next nav attach to the server we failed to stop");
  assert.equal(ownsChild(next), true, "it must remain stoppable");
});

test("shutdown's DEFAULT grace lets a slow-but-compliant server exit on SIGTERM", async () => {
  // nh drains in-flight tasks on SIGTERM, so a short grace hard-kills real work.
  // This child honours SIGTERM after 3s — well within the 10s default, far
  // outside a 2s one.
  const slow = { pid: 20, exitCode: null, signalCode: null, killed: false,
                 once() {}, kill() {}, diesOn: "SIGTERM" };
  const lc = createServerLifecycle({
    probe: async () => "down",
    stopServer: (st) => { setTimeout(() => { st.child.signalCode = "SIGTERM"; }, 3000); return true; },
    forceStopServer: (st) => { st.child.signalCode = "SIGKILL"; return true; },
    sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
  });
  lc.track(slow);
  lc.state = { status: "spawned", child: slow };
  await lc.shutdown();                     // DEFAULT grace on purpose
  assert.equal(slow.signalCode, "SIGTERM",
    "a 2s grace SIGKILLs a server that was shutting down cleanly, losing work");
});

// A child can be the CURRENT server without being in the registry — the
// defensive `targets.push(state.child)` in shutdown()/restart() covers that.
// Both branches were previously invisible: reverting them left the suite green.
// The discriminating case is a state-only child that IGNORES SIGTERM: without
// the branch, targets is empty, so both calls return "done" instantly and never
// escalate, leaving the server alive holding the port.
test("shutdown: escalates a current server that was never tracked", async () => {
  const lc = lifecycleFor([]);
  const stubborn = fakeChild(77, { diesOn: "SIGKILL" });
  lc.state = { status: "spawned", child: stubborn };
  assert.equal(lc.owned.size, 0, "precondition: not in the registry");

  await lc.shutdown(50, 5);
  await nap(5);        // death is asynchronous: the escalation lands a tick later

  assert.equal(stubborn.signalCode, "SIGKILL",
    "a state-only child must still be escalated, not abandoned at quit");
});

test("restart: waits for and escalates a current server that was never tracked",
  async () => {
    const lc = lifecycleFor([]);
    const stubborn = fakeChild(78, { diesOn: "SIGKILL" });
    lc.state = { status: "spawned", child: stubborn };

    const ok = await lc.restart(500, 5, 20);

    assert.equal(stubborn.signalCode, "SIGKILL",
      "restart must not report success while an untracked child still holds the port");
    assert.equal(ok, true);
  });

// heldStopFailed(): the guard that stops a Retry silently attaching the board to
// a server we FAILED to stop — which still holds the OLD token.
test("heldStopFailed: true only while a stop-failed child is still alive", () => {
  const lc = lifecycleFor([]);
  assert.equal(lc.heldStopFailed(), false, "no state at all");

  lc.state = { status: "failed", reason: "spawn-timeout", child: fakeChild(1) };
  assert.equal(lc.heldStopFailed(), false, "a different failure is not stop-failed");

  const child = fakeChild(2);
  lc.state = { status: "failed", reason: "stop-failed", child };
  assert.equal(lc.heldStopFailed(), true, "alive and unstoppable — must not attach");

  child.exitCode = 0;                       // it finally died
  assert.equal(lc.heldStopFailed(), false,
    "once it exits the port is free and attaching is correct again");

  lc.state = { status: "attached", reason: "stop-failed", child: fakeChild(3) };
  assert.equal(lc.heldStopFailed(), false, "an attached server was never ours");
});

test("track: a child that exits is evicted from the registry", () => {
  const lc = lifecycleFor([]);
  const child = fakeChild(30);
  lc.track(child);
  assert.equal(lc.owned.size, 1);
  assert.ok(child.handlers.exit, "track() must subscribe to the child's exit");

  child.exitCode = 0;
  child.emit("exit");

  assert.equal(lc.owned.size, 0,
    "a dead child left in the registry makes ownsAny() true forever, so quit " +
    "waits on a server that no longer exists");
});

test("restart: our child exiting is not enough — the port must be free too", () => {
  // If another process still holds the port, returning true here makes
  // ensureServer probe up and ATTACH: the board then runs on a foreign server
  // with the old token, and we no longer hold a handle to anything.
  const gone = fakeChild(40);
  const lc = createServerLifecycle({
    probe: async () => "up",                    // somebody else still serving
    stopServer: () => { gone.signalCode = "SIGTERM"; return true; },
    forceStopServer: () => true,
    sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
  });
  lc.track(gone);
  lc.state = { status: "spawned", child: gone };
  return lc.restart(120, 5, 1000).then((ok) => {
    assert.equal(ok, false,
      "reported success while a foreign server still held the port");
  });
});

// The two mirror-image defects that shipped in consecutive rounds. Each of the
// single predicates below is correct in ONE of these states and wrong in the
// other; ownsAnyAlive() must be right in both.
test("ownsAnyAlive: true mid-boot, when the child is tracked but state is null", () => {
  const lc = lifecycleFor([]);
  lc.track(fakeChild(50));
  assert.equal(lc.state, null, "precondition: ensureServer has not returned yet");
  assert.equal(lc.ownsAnyAlive(), true,
    "a boot in flight IS ours; missing it told the user 'Connected' over a " +
    "server still holding the OLD token");
});

test("ownsAnyAlive: false once our child has died, even though state still holds it", () => {
  // track()'s exit listener evicts from `owned` but never clears `state`, so a
  // corpse lives in state forever. ownsAny() therefore stays true and a foreign
  // server on the port gets blamed on us: the user is told to quit no_human,
  // which cannot free a port another process holds.
  const dead = fakeChild(51);
  const lc = lifecycleFor([dead]);
  lc.state = { status: "spawned", child: dead };
  dead.exitCode = 0;
  dead.emit("exit");

  assert.equal(lc.ownsAny(), true, "ownsChild() has no liveness check — this is the trap");
  assert.equal(lc.ownsAnyAlive(), false,
    "a dead child must not make us claim the port; the server answering it is " +
    "somebody else's and only THEY can restart it");
});

test("ownsAnyAlive: a live child outranks a dead one", () => {
  const dead = fakeChild(52), live = fakeChild(53);
  const lc = lifecycleFor([dead, live]);
  dead.exitCode = 0;
  assert.equal(lc.ownsAnyAlive(), true, "one survivor is enough to still own the port");
});
