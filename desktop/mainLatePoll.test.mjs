// The non-latching re-probe's edge cases, unit-tested in isolation.
//
// mainLateServer.test.mjs proves the happy path end-to-end through a real
// spawned server. These drive main.mjs's exported pollForLateServer directly
// with injected deps, so the awkward paths the devil's advocate asks about —
// the user navigating away mid-poll, the window torn down, the port flapping,
// and a server that never comes up — are each exercised without spawning
// anything. main.mjs is imported but app-ready is NEVER fired, so no window and
// no startup nav run; only the pure function under test does.
import { register } from "node:module";
import assert from "node:assert/strict";
import test from "node:test";
import os from "node:os";

register("./testing/electronLoader.mjs", import.meta.url);
process.env.HOME = os.tmpdir();
process.env.USERPROFILE = os.tmpdir();
process.env.NH_ORIGIN = "http://127.0.0.1:19999"; // never actually probed here

const rejections = [];
process.on("unhandledRejection", (e) => rejections.push(e));

const { pollForLateServer } = await import("./main.mjs");

const liveWin = { isDestroyed: () => false };
const always = () => true;
const fast = { windowMs: 4000, intervalMs: 10 };

test("loads the board the first time the re-probe answers 'up'", async () => {
  let loaded = 0;
  await pollForLateServer(liveWin, always, {
    ...fast, probeOrigin: async () => "up", loadBoard: async () => { loaded += 1; return true; },
  });
  assert.equal(loaded, 1, "a server that answers must load the board exactly once");
});

test("stands down without painting when the nav is superseded mid-poll", async () => {
  // current() going false is how _loadBoardOrError signals that something newer
  // owns the window (the credential screen opened, or Retry started a new nav).
  // The poll must NOT load the board over it, even though the server is 'up'.
  let loaded = 0;
  await pollForLateServer(liveWin, () => false, {
    ...fast, probeOrigin: async () => "up", loadBoard: async () => { loaded += 1; return true; },
  });
  assert.equal(loaded, 0, "the poll painted the board over a screen the user navigated to");
});

test("stops cleanly when the window is torn down mid-poll", async () => {
  let loaded = 0;
  await pollForLateServer({ isDestroyed: () => true }, always, {
    ...fast, probeOrigin: async () => "up", loadBoard: async () => { loaded += 1; return true; },
  });
  assert.equal(loaded, 0, "the poll tried to paint into a destroyed window");
});

test("a probe that flaps up→down (load fails once) is not abandoned — it keeps trying", async () => {
  // The server answered the probe but the port had flapped down by the load, so
  // showBoard returns false (it renders load-failed, never a blank window). The
  // poll must keep probing so a genuine late boot still recovers.
  const loadResults = [false, true];
  let probes = 0, loads = 0;
  await pollForLateServer(liveWin, always, {
    ...fast,
    probeOrigin: async () => { probes += 1; return "up"; },
    loadBoard: async () => { loads += 1; return loadResults[loads - 1] ?? true; },
  });
  assert.equal(loads, 2, "a single flap must not end the poll — it must try again");
  assert.ok(probes >= 2, "the poll must re-probe after a failed load");
});

test("a server that never comes up ends within the window and loads nothing (no leaked timer)", async () => {
  let loaded = 0;
  const t0 = Date.now();
  await pollForLateServer(liveWin, always, {
    windowMs: 120, intervalMs: 20,
    probeOrigin: async () => "down", loadBoard: async () => { loaded += 1; return true; },
  });
  assert.equal(loaded, 0, "a server that never answered must not load the board");
  assert.ok(Date.now() - t0 >= 120, "the poll must run out its bounded window, not exit early");
  assert.ok(Date.now() - t0 < 4000, "the poll must STOP at the window, not run forever");
});

test("the poll never leaks an unhandled rejection", () => {
  assert.deepEqual(rejections.map((e) => (e && e.message) || String(e)), []);
});
