import assert from "node:assert/strict";
import test from "node:test";

import { quitAction } from "./quitPolicy.mjs";

test("holds the quit while a shutdown is in flight", () => {
  assert.equal(quitAction({ ownsAny: true, shuttingDown: false, shutdownDone: false }),
    "delay");
  // The bug: a second Cmd-Q fell through, quit immediately, and abandoned the
  // pending SIGKILL escalation — the server survived the app.
  assert.equal(quitAction({ ownsAny: true, shuttingDown: true, shutdownDone: false }),
    "keep-held");
});

test("lets the quit through once shutdown has finished", () => {
  assert.equal(quitAction({ ownsAny: true, shuttingDown: true, shutdownDone: true }),
    "proceed");
});

test("never delays a quit when we own nothing", () => {
  assert.equal(quitAction({ ownsAny: false, shuttingDown: false, shutdownDone: false }),
    "proceed");
});
