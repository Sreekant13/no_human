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
