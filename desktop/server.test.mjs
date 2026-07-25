// Unit tests for the shell's server-discovery helpers (node --test, no
// electron needed — these run in the standing web gate's node tier).
import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import test from "node:test";

import {
  classifyBackendFailure, configuredPort, isAppOrigin, makeOutputCapture,
  probe, tailDetail, waitForServer,
} from "./server.mjs";

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

import { CLI_HINT_DIRS, bundledNhPath, ensureServer, mergePath, resolveNhBin, stopServer } from "./server.mjs";
import { mkdirSync } from "node:fs";
import { mkdtempSync, writeFileSync, chmodSync, readFileSync, existsSync } from "node:fs";
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

test("bundledNhPath: finds Resources/nh-server/nh, empty when absent or unpackaged", () => {
  const res = mkdtempSync(join(tmpdir(), "nhres-"));
  assert.equal(bundledNhPath(res), "", "no bundle yet → empty");
  mkdirSync(join(res, "nh-server"));
  writeFileSync(join(res, "nh-server", "nh"), "#!/bin/sh\nexit 0\n");
  assert.equal(bundledNhPath(res), join(res, "nh-server", "nh"));
  // Outside Electron process.resourcesPath is undefined — must not throw.
  assert.equal(bundledNhPath(undefined), "");
});

test("resolveNhBin: bundled nh wins over PATH, but NH_BIN still wins over bundled", async () => {
  const dir = mkdtempSync(join(tmpdir(), "nhbin-"));
  const bundled = join(dir, "bundled-nh");
  writeFileSync(bundled, "#!/bin/sh\nexit 0\n");
  chmodSync(bundled, 0o755);
  // No NH_BIN → the bundle is used instead of the login shell's nh.
  assert.equal(await resolveNhBin({ SHELL: "/usr/bin/false" }, [], bundled),
               bundled);
  // Explicit NH_BIN outranks the bundle (documented escape hatch).
  const override = join(dir, "override-nh");
  writeFileSync(override, "#!/bin/sh\nexit 0\n");
  chmodSync(override, 0o755);
  assert.equal(await resolveNhBin({ NH_BIN: override, SHELL: "/usr/bin/false" },
                                  [], bundled), override);
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
    // Deadline, not a fixed wait: ensureServer returns as soon as the port
    // answers (~400ms idle). A roomier deadline only affects a genuinely slow
    // boot under load, which is the flake this widens out (gate finding #2).
    origin, spawnTimeoutMs: 20000, env: { NH_BIN: fake }, nhArgs: [] });
  try {
    assert.equal(state.status, "spawned");
    assert.ok(state.child.pid > 0);
  } finally {
    stopServer(state);
  }
});

test("ensureServer: onSpawn fires immediately, not when the wait finishes", async () => {
  const dir = mkdtempSync(join(tmpdir(), "nhbin-"));
  const fake = join(dir, "nh");
  writeFileSync(fake, "#!/bin/sh\nsleep 30\n");
  chmodSync(fake, 0o755);
  const seen = [];
  const t0 = Date.now();
  const state = await ensureServer({
    origin: "http://127.0.0.1:1", spawnTimeoutMs: 900, env: { NH_BIN: fake },
    nhArgs: [], onSpawn: (c) => seen.push({ pid: c.pid, at: Date.now() - t0 }) });
  try {
    assert.equal(seen.length, 1, "the caller must be able to track it at once");
    assert.ok(seen[0].pid > 0);
    assert.ok(seen[0].at < 500,
      `onSpawn must fire at spawn time, fired at ${seen[0].at}ms`);
  } finally { stopServer(state); }
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
  // A "failed" state that still carries a child IS ours: ensureServer leaves a
  // slow-booting nh running and it may yet bind the port. Refusing to stop it
  // orphaned the process (reparented to init, still holding the port).
  assert.equal(stopServer({ status: "failed", reason: "nh-not-found" }), false,
    "no child -> nothing to stop");
  assert.equal(killed, false, "attached must never kill");
  assert.equal(stopServer({ status: "spawned", child }), true);
  assert.equal(killed, true);

  // An already-exited child is not "stopped" — reporting true inflated the
  // drain count and preceded a group kill on a possibly-reused PID.
  assert.equal(stopServer({ status: "spawned",
    child: { pid: 999999, exitCode: 0, signalCode: null, kill: () => {} } }), false);

  let killed2 = false;
  const child2 = { kill: () => { killed2 = true; } };
  assert.equal(stopServer({ status: "failed", reason: "spawn-timeout", child: child2 }),
               true, "a spawn-timeout child is ours to stop, not an orphan");
  assert.equal(killed2, true);
});

// --- the spawned server must be able to find `claude` ------------------------
// The Agent SDK resolves its CLI off PATH. A macOS GUI app inherits launchd's
// PATH, so without this the packaged build serves a healthy board on which
// EVERY task dies with CLINotFoundError.

test("mergePath: appends missing dirs, never reorders or duplicates", () => {
  const exists = () => true;
  assert.equal(mergePath("/usr/bin:/bin", ["/opt/homebrew/bin"], exists),
    "/usr/bin:/bin:/opt/homebrew/bin", "hint dirs go AFTER the inherited PATH");
  assert.equal(mergePath("/usr/bin:/opt/homebrew/bin", ["/opt/homebrew/bin"], exists),
    "/usr/bin:/opt/homebrew/bin", "an entry already present is not duplicated");
  assert.equal(mergePath("", ["/opt/homebrew/bin"], exists), "/opt/homebrew/bin");
});

test("mergePath: never appends a directory that does not exist", () => {
  assert.equal(mergePath("/usr/bin", ["/nope/nowhere"], () => false), "/usr/bin");
});

test("mergePath: makes a Homebrew claude reachable from launchd's PATH", () => {
  // The real failure: /opt/homebrew/bin is in NEITHER launchd's default PATH
  // nor the SDK's hardcoded fallback list, so a Homebrew install is invisible.
  const launchd = "/usr/bin:/bin:/usr/sbin:/sbin";
  const onlyBrew = (d) => d === "/opt/homebrew/bin";
  assert.ok(mergePath(launchd, CLI_HINT_DIRS, onlyBrew).split(":")
    .includes("/opt/homebrew/bin"),
    "a Homebrew-installed claude must be reachable by the spawned server");
});

test("ensureServer: the spawned server inherits the widened PATH", async () => {
  const dir = mkdtempSync(join(tmpdir(), "nhpath-"));
  const port = 18400 + (process.pid % 400);
  const out = join(dir, "path.txt");
  const fake = join(dir, "nh");
  // Absolute shebang: the test PATH below is deliberately too narrow to find
  // `node`, and the shebang must not be what this test is measuring.
  writeFileSync(fake, `#!${process.execPath}
require("node:fs").writeFileSync(${JSON.stringify(out)}, process.env.PATH || "");
const http = require("node:http");
http.createServer((req, res) => res.end("[]")).listen(${port}, "127.0.0.1");
setInterval(() => {}, 1000);
`);
  chmodSync(fake, 0o755);
  // LAUNCHD'S PATH. Running under a normal shell the hint dirs are already
  // inherited, so the widening is invisible and the test proves nothing —
  // which is exactly the reason this defect shipped.
  const state = await ensureServer({
    origin: "http://127.0.0.1:" + port, spawnTimeoutMs: 8000,
    env: { NH_BIN: fake, PATH: "/usr/bin:/bin:/usr/sbin:/sbin" }, nhArgs: [] });
  try {
    assert.equal(state.status, "spawned");
    const childPath = readFileSync(out, "utf8").split(":");
    const expected = CLI_HINT_DIRS.filter((d) => existsSync(d));
    assert.ok(expected.length > 0, "no hint dir exists here; the test proves nothing");
    for (const d of expected) {
      assert.ok(childPath.includes(d),
        `the spawned server cannot see ${d}, so the Agent SDK cannot find claude`);
    }
  } finally {
    stopServer(state);
  }
});

// --- configuredPort: a hand-rolled YAML block parser with no tests at all ----
// docs/INSTALLER.md's verification recipe depends on it, and the shell must
// find the same port `nh start` binds or a configured install is stranded.

test("configuredPort: reads server.port, and only inside the server block", () => {
  const dir = mkdtempSync(join(tmpdir(), "nhcfg-"));
  const write = (body) => {
    const f = join(dir, `${Math.random().toString(36).slice(2)}.yaml`);
    writeFileSync(f, body); return f;
  };
  assert.equal(configuredPort(write("server:\n  port: 18994\n")), 18994);
  assert.equal(configuredPort(write("server:\n  host: 127.0.0.1\n  port: 9\n")), 9);
  // A port under a DIFFERENT top-level key must not be picked up.
  assert.equal(configuredPort(write("llm:\n  port: 1234\n")), 8420);
  // The block ends at the next top-level key.
  assert.equal(configuredPort(write("server:\n  host: x\nllm:\n  port: 1234\n")), 8420);
});

test("configuredPort: falls back to 8420 rather than throwing", () => {
  const dir = mkdtempSync(join(tmpdir(), "nhcfg-"));
  assert.equal(configuredPort(join(dir, "absent.yaml")), 8420, "no config file");
  const f = join(dir, "junk.yaml");
  writeFileSync(f, "not: [valid\n  yaml\n");
  assert.equal(configuredPort(f), 8420, "unparseable config must not crash the shell");
});

test("stopServer: kills the process GROUP, not just the direct child", async () => {
  // This is the entire reason for detached:true. Signalling only the direct
  // child left nh's workers alive at PPID 1, still holding the port.
  const dir = mkdtempSync(join(tmpdir(), "nhgrp-"));
  const marker = join(dir, "grandchild.pid");
  const fake = join(dir, "nh");
  writeFileSync(fake, `#!${process.execPath}
const { spawn } = require("node:child_process");
const kid = spawn(${JSON.stringify(process.execPath)}, ["-e", "setInterval(()=>{},1000)"]);
require("node:fs").writeFileSync(${JSON.stringify(marker)}, String(kid.pid));
setInterval(() => {}, 1000);
`);
  chmodSync(fake, 0o755);
  const state = await ensureServer({
    origin: "http://127.0.0.1:1", spawnTimeoutMs: 700,
    env: { NH_BIN: fake }, nhArgs: [] });
  const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };
  // Poll for the worker's BIRTH as well as its death: a bare read here went
  // ENOENT under parallel load and produced a random red build.
  for (let i = 0; i < 60 && !existsSync(marker); i++) {
    await new Promise((r) => setTimeout(r, 50));
  }
  assert.ok(existsSync(marker), "the fake server never started its worker");
  const grandchild = Number(readFileSync(marker, "utf8"));
  assert.ok(alive(grandchild), "the worker should be running before we stop");

  assert.equal(stopServer(state), true);
  for (let i = 0; i < 40 && (alive(state.child.pid) || alive(grandchild)); i++) {
    await new Promise((r) => setTimeout(r, 50));
  }
  assert.equal(alive(grandchild), false,
    "the worker outlived the server it belonged to and still holds the port");
  assert.equal(alive(state.child.pid), false, "the server itself survived");
});

// ── SCRUM-11: launch-failure capture/classification (review finding: the
//    feature's server half shipped untested; these pin the contract) ──────────
test("classifyBackendFailure: cli-missing on the _assert_backend_usable text", () => {
  const out = "coding backend unavailable: the `claude` CLI was not found.\nFix: install";
  assert.equal(classifyBackendFailure(out), "cli-missing");
});

test("classifyBackendFailure: not-logged-in on the two missing-token AuthErrors only", () => {
  assert.equal(classifyBackendFailure(
    "auth error: No subscription token found. Expected CLAUDE_CODE_OAUTH_TOKEN in ~/.no_human/.env"),
    "not-logged-in");
  assert.equal(classifyBackendFailure(
    "auth error: auth profile 'work' has no token. Expected CLAUDE_CODE_OAUTH_TOKEN_WORK in ~/.no_human/.env"),
    "not-logged-in");
  // Other AuthErrors print a "claude setup-token" Fix block too, but
  // setup-token is the WRONG remediation — they must stay unclassified.
  assert.equal(classifyBackendFailure(
    "auth error: ANTHROPIC_API_KEY is set — subscription mode runs on CLAUDE_CODE_OAUTH_TOKEN only.\n" +
    "Fix: run nh init, or:\n  1. claude setup-token  (creates a subscription token)"),
    null);
});

test("classifyBackendFailure: cli-missing wins when both banners appear; unrelated output is null", () => {
  assert.equal(classifyBackendFailure(
    "coding backend unavailable: the `claude` CLI was not found.\nNo subscription token found."),
    "cli-missing");
  assert.equal(classifyBackendFailure("Address already in use: 8420"), null);
  assert.equal(classifyBackendFailure(""), null);
});

test("tailDetail: keeps the last lines, caps chars, empty-safe", () => {
  assert.equal(tailDetail(""), "");
  assert.equal(tailDetail("one\ntwo"), "one\ntwo");
  const many = Array.from({ length: 30 }, (_, i) => `line${i}`).join("\n");
  const tail = tailDetail(many);
  assert.ok(tail.startsWith("line20"), tail);
  assert.ok(!tail.includes("line19"));
  const long = "x".repeat(2000);
  const capped = tailDetail(long);
  assert.ok(capped.length <= 500 + "[truncated…] ".length);
  assert.ok(capped.startsWith("[truncated…] "));
});

test("makeOutputCapture: bounded — keeps only the tail past the cap", () => {
  const cap = makeOutputCapture(10);
  cap.add("aaaaa"); cap.add("bbbbb"); cap.add("ccccc");
  assert.equal(cap.text(), "bbbbbccccc");
});

// ── SCRUM-49: destroy spawn-capture pipes once the server is confirmed up ──
test("makeOutputCapture: stop() releases the buffer and ignores further chunks", () => {
  const cap = makeOutputCapture(100);
  cap.add("hello");
  assert.equal(cap.text(), "hello");
  assert.equal(cap.capturing, true);
  cap.stop();
  assert.equal(cap.capturing, false);
  assert.equal(cap.text(), "", "the buffer must be released once capture stops");
  cap.add("more, after stop");
  assert.equal(cap.text(), "", "no bytes retained after stop()");
});

test("ensureServer: failure-window capture still classifies real spawn output (diagnostics survive)", async () => {
  // Proves the diagnosis path stays intact for the window this ticket does
  // NOT touch: a launch that never comes up must still surface why.
  const dir = mkdtempSync(join(tmpdir(), "nhfail-"));
  const fake = join(dir, "nh");
  writeFileSync(fake, "#!/bin/sh\necho 'coding backend unavailable: cli missing'\nexit 2\n");
  chmodSync(fake, 0o755);
  const state = await ensureServer({
    origin: "http://127.0.0.1:1", env: { NH_BIN: fake }, nhArgs: [],
    spawnTimeoutMs: 2000 });
  assert.equal(state.status, "failed");
  assert.equal(state.reason, "backend-cli-missing");
  assert.ok(state.detail.includes("coding backend unavailable"),
    `expected the captured diagnosis in detail, got: ${state.detail}`);
});

test("ensureServer: a silent fast non-zero exit is labeled backend-exited, not spawn-timeout", async () => {
  // No output at all, so classifyBackendFailure(text) returns null and the
  // reason falls through to the raw race outcome. Uses a LARGE
  // spawnTimeoutMs: if the race still keyed off the old fallback (or 'close'
  // never fired), this assertion would fail only after a ~20s hang — so a
  // fast pass here proves the race resolves on 'close' within milliseconds.
  const dir = mkdtempSync(join(tmpdir(), "nhexit-"));
  const fake = join(dir, "nh");
  writeFileSync(fake, "#!/bin/sh\nexit 3\n");
  chmodSync(fake, 0o755);
  const state = await ensureServer({
    origin: "http://127.0.0.1:1", env: { NH_BIN: fake }, nhArgs: [],
    spawnTimeoutMs: 20000 });
  assert.equal(state.status, "failed");
  assert.equal(state.reason, "backend-exited");
  assert.equal(state.detail, "", "silent exit must not fabricate a diagnosis");
});

test("ensureServer: stops capturing once confirmed up, but keeps draining so the running server never EPIPEs", async () => {
  // A shell script, not node: on a broken pipe the shell's default SIGPIPE
  // action TERMINATES it on the very next write — Node silently swallows
  // EPIPE on stdout/stderr and would not catch a regression to destroy().
  const dir = mkdtempSync(join(tmpdir(), "nhdrain-"));
  const port = 19000 + (process.pid % 900);
  const fake = join(dir, "nh");
  writeFileSync(fake, `#!/bin/sh
node -e "require('node:http').createServer(function(q,r){r.end('[]')}).listen(${port},'127.0.0.1')" &
i=0
while [ $i -lt 400 ]; do
  echo "log line $i"
  i=$((i+1))
  sleep 0.02
done
`);
  chmodSync(fake, 0o755);
  const origin = "http://127.0.0.1:" + port;
  const state = await ensureServer({
    origin, spawnTimeoutMs: 8000, env: { NH_BIN: fake }, nhArgs: [] });
  try {
    assert.equal(state.status, "spawned");
    const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };
    assert.ok(alive(state.child.pid), "server just confirmed up must still be running");
    // Several more write cycles PAST confirmation — exactly the post-startup
    // window this ticket is about (the server keeps logging long after boot).
    await new Promise((r) => setTimeout(r, 500));
    assert.ok(alive(state.child.pid),
      "the still-running server must not die from EPIPE after its pipes are released");
  } finally {
    stopServer(state);
  }
});

test("reason strings match error.html's remediation blocks (cross-file contract)", () => {
  const html = fs.readFileSync(new URL("./error.html", import.meta.url), "utf8");
  // server.mjs emits backend-cli-missing / backend-not-logged-in; error.html
  // must route each to a dedicated steps block. A typo on either side breaks
  // this pin, not just the live behavior.
  assert.ok(html.includes('"backend-cli-missing"'));
  assert.ok(html.includes('"backend-not-logged-in"'));
  assert.ok(html.includes('id="steps-cli-missing"'));
  assert.ok(html.includes('id="steps-not-logged-in"'));
});
