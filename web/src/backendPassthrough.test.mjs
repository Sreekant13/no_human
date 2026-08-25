import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import http from "node:http";

import { createTask } from "./api.js";

// Criterion 5: the backend the board's picker chose must be the backend the
// SERVER RECEIVED on the wire, not something recomputed from a component's
// own state. `backlogJira.test.mjs`'s "App.jsx forwards the composer's chosen
// backend into createTask, unrecomputed" test pins the same intent with a
// bare source regex — real, but it never EXECUTES App.jsx's line, so a
// passthrough that is present-but-wrong (reads the wrong field, sends the
// wrong key alongside a coincidentally-matching comment) would still match
// the regex. This file runs the real hop: App.jsx's own createTask(...)
// argument, evaluated for real, handed to the real `createTask` from api.js,
// which POSTs to a real `node:http` server — and the assertions read the
// body THAT SERVER RECEIVED, exactly as criterion 5 requires.
//
// This is the CI-run lane: `npm test` = `node --test src/*.test.mjs`
// (web/package.json), invoked by `.github/workflows/ci.yml:281` and
// `.gitlab-ci.yml:57`. `web/e2e/*` and `web/tests/sdlc-ui.spec.js` (Playwright)
// are NOT invoked by any CI lane today — grep both CI configs for `e2e` or
// `playwright` and get zero hits — so a Playwright-only proof would stay dark
// in exactly the way this ticket exists to fix. No browser lane is added
// here; that would be a scope expansion this ticket forbids.

const here = fileURLToPath(new URL(".", import.meta.url));
const APP_JSX_PATH = here + "App.jsx";

/** Re-read from disk every call — never cache, so an in-memory mutation of
 * the source text (used by the removal/rename controls below) can't leak
 * into a later, unrelated read. */
function readAppJsx() {
  return readFileSync(APP_JSX_PATH, "utf8");
}

/**
 * Extract the object-literal argument of App.jsx's `await createTask(...)`
 * call by a brace-balanced scan, not a lazy regex — a lazy `/createTask\(\{([\s\S]*?)\}\)/`
 * would stop at the FIRST `}`, which in this literal belongs to
 * `grillResult?.acceptance_criteria || []`'s enclosing arrow-less object, or
 * to any nested brace, and would silently truncate the payload.
 */
function extractCreateTaskLiteral(source) {
  const marker = "await createTask(";
  const start = source.indexOf(marker);
  assert.ok(start !== -1, "await createTask( was not found in App.jsx");
  let i = start + marker.length;
  while (/\s/.test(source[i])) i++;
  assert.equal(source[i], "{",
    "createTask's argument must be an object literal for this extractor to work");
  let depth = 0;
  let j = i;
  for (; j < source.length; j++) {
    if (source[j] === "{") depth++;
    else if (source[j] === "}") {
      depth--;
      if (depth === 0) { j++; break; }
    }
  }
  assert.equal(depth, 0, "unbalanced braces while scanning createTask's argument");
  return source.slice(i, j);
}

/**
 * Evaluate the extracted literal for real, against caller-supplied inputs —
 * the same free identifiers App.jsx's handleSubmit closes over: `fields`,
 * `grillResult`, and the locally-computed `title`/`description`.
 */
function buildPayload(literal, { fields, grillResult, title, description }) {
  const build = new Function(
    "fields", "grillResult", "title", "description",
    `"use strict"; return (${literal});`,
  );
  return build(fields, grillResult, title, description);
}

// The extractor must actually work, and must not pass vacuously — assert it
// found the real literal (carrying keys no stub would produce by accident).
test("the extractor finds App.jsx's real createTask(...) literal", () => {
  const literal = extractCreateTaskLiteral(readAppJsx());
  assert.match(literal, /repo_path:\s*fields\.repoPath/);
  assert.match(literal, /backend:\s*fields\.backend/);
  const payload = buildPayload(literal, {
    fields: { repoPath: "/r", backend: "codex" },
    grillResult: null,
    title: "t",
    description: "d",
  });
  assert.equal(payload.repo_path, "/r");
  assert.equal(payload.backend, "codex");
});

// ── real server, real wire ───────────────────────────────────────────────

let server;
let origin;
let recorded;
const originalFetch = globalThis.fetch;

test.before(async () => {
  server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (chunk) => { raw += chunk; });
    req.on("end", () => {
      let body;
      try { body = raw ? JSON.parse(raw) : undefined; } catch { body = undefined; }
      recorded.push({ method: req.method, url: req.url, body });
      res.writeHead(201, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ id: "t1" }));
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
  // api.js's BASE is "" on purpose (its own comment: importable outside a
  // browser), so `fetch("/api/tasks", ...)` is a relative URL — Node's real
  // fetch, unlike a browser, has no page origin to resolve it against. This
  // wrapper supplies exactly that resolution and nothing else: the request
  // method/headers/body all reach the REAL fetch untouched.
  globalThis.fetch = (url, opts) => {
    const absolute = typeof url === "string" && url.startsWith("/") ? `${origin}${url}` : url;
    return originalFetch(absolute, opts);
  };
});

test.after(async () => {
  globalThis.fetch = originalFetch;
  await new Promise((resolve) => server.close(resolve));
});

/** Build App.jsx's real payload, POST it through the real createTask, and
 * return what the server actually received. */
async function postThroughApp({ fields, grillResult = null, title = "t", description = "d" }) {
  const literal = extractCreateTaskLiteral(readAppJsx());
  const payload = buildPayload(literal, { fields, grillResult, title, description });
  recorded = [];
  await createTask(payload);
  assert.equal(recorded.length, 1, "expected exactly one POST /api/tasks");
  return recorded[0];
}

test("the backend the UI chose is the backend on the POST /api/tasks body the server received", async () => {
  for (const chosen of ["claude", "codex", "local"]) {
    const received = await postThroughApp({
      fields: { repoPath: "/r", backend: chosen, kind: "code_change", priority: "normal", source: "board" },
    });
    assert.equal(received.method, "POST");
    assert.equal(received.body.backend, chosen);
  }
});

test("the value is passed through, never recomputed", async () => {
  // Every field a wrong implementation might accidentally read instead of
  // `fields.backend` gets its OWN distinct sentinel — if the server-received
  // `backend` ever matched one of these, the passthrough would be reading
  // the wrong field, not forwarding the chosen one.
  const received = await postThroughApp({
    fields: {
      repoPath: "/r",
      backend: "codex",
      kind: "sentinel-kind",
      priority: "sentinel-priority",
      source: "sentinel-source",
      projectId: "sentinel-project",
    },
    title: "sentinel-title",
    description: "sentinel-description",
  });
  assert.equal(received.body.backend, "codex");
  const others = [received.body.kind, received.body.priority, received.body.source,
    received.body.project_id, received.body.title, received.body.description];
  assert.ok(!others.includes("codex"),
    "no other field carries the chosen backend's value, so it can't be a copy of one of them");
});

test("an untouched picker reaches the server as an empty backend (worker.backend default)", async () => {
  const received = await postThroughApp({
    fields: { repoPath: "/r", backend: "", kind: "code_change", priority: "normal", source: "board" },
  });
  assert.ok(Object.prototype.hasOwnProperty.call(received.body, "backend"),
    "the key must still be present — the server's `if body.backend:` fallback depends on that");
  assert.equal(received.body.backend, "");
});

// ── mutation controls — the RED proof that the above isn't vacuous ─────────

test("removing App.jsx's passthrough makes the wire body lose the backend", async () => {
  const mutated = readAppJsx().replace("backend: fields.backend,\n", "");
  assert.notEqual(mutated, readAppJsx(), "the mutation must actually change the source");
  const literal = extractCreateTaskLiteral(mutated);
  assert.doesNotMatch(literal, /backend:\s*fields\.backend/,
    "the mutant must actually remove the passthrough this test scans for");
  const payload = buildPayload(literal, {
    fields: { repoPath: "/r", backend: "codex", kind: "code_change", priority: "normal", source: "board" },
    grillResult: null, title: "t", description: "d",
  });
  recorded = [];
  await createTask(payload);
  assert.equal(recorded.length, 1);
  assert.equal(recorded[0].body.backend, undefined);
});

test("renaming App.jsx's passthrough makes the wire body lose the backend", async () => {
  const mutated = readAppJsx().replace("backend: fields.backend,", "coderBackend: fields.backend,");
  assert.notEqual(mutated, readAppJsx(), "the mutation must actually change the source");
  const literal = extractCreateTaskLiteral(mutated);
  assert.doesNotMatch(literal, /backend:\s*fields\.backend/,
    "the mutant must actually rename the passthrough this test scans for");
  const payload = buildPayload(literal, {
    fields: { repoPath: "/r", backend: "codex", kind: "code_change", priority: "normal", source: "board" },
    grillResult: null, title: "t", description: "d",
  });
  // Sanity: the rename must reach createTask's PAYLOAD (proving the mutant
  // took) even though api.js's own allow-list drops any key it doesn't name —
  // so `coderBackend` never reaches the wire, only confirming `backend` is
  // gone from it.
  assert.equal(payload.coderBackend, "codex");
  assert.equal(payload.backend, undefined);
  recorded = [];
  await createTask(payload);
  assert.equal(recorded.length, 1);
  assert.equal(recorded[0].body.backend, undefined);
  assert.equal(recorded[0].body.coderBackend, undefined,
    "api.js's allow-list must not have grown a coderBackend passthrough on its own");
});
