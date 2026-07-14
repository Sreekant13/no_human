// Unit tests for the shell's server-discovery helpers (node --test, no
// electron needed — these run in the standing web gate's node tier).
import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";

import { isAppOrigin, probe, waitForServer } from "./server.mjs";

function serve(handler) {
  return new Promise((resolve) => {
    const srv = http.createServer(handler);
    srv.listen(0, "127.0.0.1", () =>
      resolve({ srv, origin: `http://127.0.0.1:${srv.address().port}` }));
  });
}

test("probe: up only when /api/tasks answers 2xx", async () => {
  const { srv, origin } = await serve((req, res) => {
    if (req.url === "/api/tasks") { res.end("[]"); return; }
    res.statusCode = 404; res.end();
  });
  try {
    assert.equal(await probe(origin), "up");
  } finally { srv.close(); }
});

test("probe: down on refused connection and on 5xx", async () => {
  assert.equal(await probe("http://127.0.0.1:1"), "down");
  const { srv, origin } = await serve((_req, res) => {
    res.statusCode = 500; res.end();
  });
  try {
    assert.equal(await probe(origin), "down");
  } finally { srv.close(); }
});

test("waitForServer: resolves true once the server comes up", async () => {
  let ready = false;
  const { srv, origin } = await serve((_req, res) => {
    if (!ready) { res.statusCode = 500; res.end(); return; }
    res.end("[]");
  });
  setTimeout(() => { ready = true; }, 300);
  try {
    assert.equal(await waitForServer(origin, 5000, 100), true);
  } finally { srv.close(); }
});

test("waitForServer: false past the deadline", async () => {
  assert.equal(await waitForServer("http://127.0.0.1:1", 400, 100), false);
});

test("isAppOrigin: same-origin stays in-window, everything else leaves", () => {
  const o = "http://127.0.0.1:8420";
  assert.equal(isAppOrigin("http://127.0.0.1:8420/stats", o), true);
  assert.equal(isAppOrigin("https://code.example.com/x/pull/1", o), false);
  assert.equal(isAppOrigin("http://127.0.0.1:9999/", o), false);
  assert.equal(isAppOrigin("not a url", o), false);
});

// ------------------------------ E2 tests ---------------------------------- //

import { ensureServer, resolveNhBin, stopServer } from "./server.mjs";
import { mkdtempSync, writeFileSync, chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

test("resolveNhBin: NH_BIN wins when it exists; missing → shell/known-path fallbacks", async () => {
  const dir = mkdtempSync(join(tmpdir(), "nhbin-"));
  const fake = join(dir, "nh");
  writeFileSync(fake, "#!/bin/sh\nexit 0\n");
  chmodSync(fake, 0o755);
  assert.equal(await resolveNhBin({ NH_BIN: fake, SHELL: "/bin/sh" }), fake);
  // A bogus NH_BIN must not be returned.
  const got = await resolveNhBin({ NH_BIN: join(dir, "missing"),
                                   SHELL: "/usr/bin/false" });
  assert.notEqual(got, join(dir, "missing"));
});

test("ensureServer: attaches without spawning when the server is up", async () => {
  const { srv, origin } = await serve((req, res) => { res.end("[]"); });
  try {
    const state = await ensureServer({ origin, env: { NH_BIN: "/nope" } });
    assert.equal(state.status, "attached");
    assert.equal(state.child, undefined);
  } finally { srv.close(); }
});

test("ensureServer: spawns a fake nh and waits until the port answers", async () => {
  // The fake `nh` starts a real HTTP server on a fixed port via node.
  const dir = mkdtempSync(join(tmpdir(), "nhbin-"));
  const port = 18000 + (process.pid % 1000);
  const fake = join(dir, "nh");
  writeFileSync(fake, `#!/usr/bin/env node
const http = require("node:http");
setTimeout(() => {
  http.createServer((req, res) => res.end("[]")).listen(${port}, "127.0.0.1");
}, 300);
setInterval(() => {}, 1000);
`);
  chmodSync(fake, 0o755);
  const origin = "http://127.0.0.1:" + port;
  const state = await ensureServer({
    origin, spawnTimeoutMs: 8000, env: { NH_BIN: fake }, nhArgs: [] });
  try {
    assert.equal(state.status, "spawned");
    assert.ok(state.child.pid > 0);
  } finally {
    stopServer(state);
  }
});

test("ensureServer: failed resolution reports nh-not-found without spawning", async () => {
  const state = await ensureServer({
    origin: "http://127.0.0.1:1",
    env: { NH_BIN: "/definitely/missing", SHELL: "/usr/bin/false" },
    fallbackPaths: [],   // this machine has a real nh — keep it out
    spawnTimeoutMs: 500 });
  assert.equal(state.status, "failed");
  assert.equal(state.reason, "nh-not-found");
});

test("stopServer: ONLY kills a spawned child — attached/failed states are never killed", () => {
  let killed = false;
  const child = { kill: () => { killed = true; } };
  assert.equal(stopServer({ status: "attached" }), false);
  // The gate itself must protect attached — even with a child present
  // (review: without this, only production convention protects attached).
  assert.equal(stopServer({ status: "attached", child }), false);
  assert.equal(stopServer({ status: "failed", child }), false);
  assert.equal(killed, false, "attached/failed must never kill");
  assert.equal(stopServer({ status: "spawned", child }), true);
  assert.equal(killed, true);
});
