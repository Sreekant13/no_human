// The `second-instance` handler had NO test at all: `grep -rn "second-instance"
// desktop/*.test.mjs` returned nothing, so every branch of it was free.
//
// THE DEFECT this pins. The handler ends in `showWindow()`, and showWindow FALLS
// THROUGH to `createWindow()` whenever `win` is null or destroyed. On darwin
// `win` is null in exactly one situation — the app is quitting — and quitting
// here is DELAYED on purpose so a server that ignores SIGTERM can be escalated
// to SIGKILL. The single-instance lock is held for the whole of that delay, so a
// launch during it arrives HERE rather than starting a fresh app, and without a
// guard it builds a brand-new window pointed at a server being torn down
// underneath it. `if (quitting) return;` is the guard.
//
// Both directions are asserted, because a guard that always returns would be
// just as wrong as no guard: reopening from the tray while the app is alive is
// the handler's actual job.
//
// The window is made to look destroyed by replacing `isDestroyed` on the object
// main.mjs is holding. That is the state that matters — it is the only way
// showWindow reaches createWindow — and the stub's own window is never
// destroyed, so without this the fall-through branch is unreachable and a test
// of it would pass against the unfixed code.
import { register } from "node:module";
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execSync } from "node:child_process";

register("./testing/electronLoader.mjs", import.meta.url);

const MARK = `nhSecond${process.pid}`;

// No token in ~/.no_human, so startup takes the setup path: token.html loads and
// nothing is spawned. NH_BIN still points at a fake so a stray resolution can
// never reach the operator's real nh, and NH_ORIGIN is non-routable.
const home = fs.mkdtempSync(path.join(os.tmpdir(), "nh-second-"));
fs.mkdirSync(path.join(home, ".no_human"));
const fakeNh = path.join(home, "nh");
fs.writeFileSync(fakeNh, `#!/usr/bin/env node
process.title = ${JSON.stringify(MARK)};
setInterval(() => {}, 1000);
`);
fs.chmodSync(fakeNh, 0o755);
process.env.HOME = home;
process.env.USERPROFILE = home;   // os.homedir() reads USERPROFILE on Windows
process.env.NH_BIN = fakeNh;
process.env.NH_ORIGIN = `http://10.255.255.1:${19980 + (process.pid % 15)}`;

const stub = await import("./testing/electronStub.mjs");
await import("./main.mjs");
stub.fireReady();
await new Promise((r) => setTimeout(r, 3000));

test.after(() => {
  try { execSync(`/usr/bin/pkill -9 -f ${MARK}`); } catch { /* already gone */ }
  fs.rmSync(home, { recursive: true, force: true });
});

test("startup registered a second-instance handler at all", () => {
  assert.ok(stub.calls.handlers.get("second-instance"),
    "main.mjs holds the single-instance lock but registers no second-instance "
    + "handler — relaunching the app would do nothing");
  assert.ok(stub.BrowserWindow.last, "no window was ever created; the rest is void");
});

test("a relaunch of the LIVE app surfaces the window", () => {
  const secondInstance = stub.calls.handlers.get("second-instance");
  const win = stub.BrowserWindow.last;
  const shownBefore = win.shown;

  secondInstance();

  assert.equal(stub.BrowserWindow.last, win, "an existing window must be reused");
  assert.equal(win.shown, shownBefore + 1,
    "show() was not called: with close-to-tray, relaunching is how a user "
    + "reopens a HIDDEN window, and focus() alone leaves it invisible");
});

test("a relaunch REBUILDS the window when the old one is gone and we are not quitting", () => {
  // The fall-through branch, established as reachable before the guard below is
  // asserted. If this fails, the "quitting" test proves nothing.
  const secondInstance = stub.calls.handlers.get("second-instance");
  const stale = stub.BrowserWindow.last;
  stale.isDestroyed = () => true;

  secondInstance();

  assert.notEqual(stub.BrowserWindow.last, stale,
    "showWindow did not fall through to createWindow for a destroyed window");
});

test("a relaunch DURING the delayed quit builds nothing", () => {
  const secondInstance = stub.calls.handlers.get("second-instance");
  const beforeQuit = stub.calls.handlers.get("before-quit");
  assert.ok(beforeQuit, "main.mjs must register a before-quit handler");

  // Latch `quitting` the way a real Cmd-Q / tray-quit does, then put the window
  // into the state it is actually in while the app tears down.
  beforeQuit({ preventDefault() {} });
  const dying = stub.BrowserWindow.last;
  dying.isDestroyed = () => true;
  const shownBefore = dying.shown;

  secondInstance();

  assert.equal(stub.BrowserWindow.last, dying,
    "a launch during the SIGTERM->SIGKILL quit delay built a FRESH window "
    + "against a server being torn down — the single-instance lock is still "
    + "held throughout that delay, so this is the path a real relaunch takes");
  assert.equal(dying.shown, shownBefore,
    "the dying window was surfaced again mid-teardown");
});
