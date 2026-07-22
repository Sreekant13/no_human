// The credential IPC gate. This is the branch's most security-sensitive check —
// without it, anything rendered by the board could overwrite the operator's
// OAuth token — so it is pinned here rather than relying on manual probing.
import assert from "node:assert/strict";
import test from "node:test";
import path from "node:path";
import { pathToFileURL } from "node:url";

import { isSetupUrl } from "./setupGate.mjs";

const SETUP = path.join("/apps", "no_human", "token.html");
const setupUrl = pathToFileURL(SETUP).href;

test("isSetupUrl: only the exact local setup file passes", () => {
  assert.equal(isSetupUrl(setupUrl, SETUP), true);
  assert.equal(isSetupUrl(`${setupUrl}?canReturn=1`, SETUP), true,
    "the query string must not defeat the check");
});

test("isSetupUrl: board origins and lookalikes are rejected", () => {
  for (const url of [
    "http://127.0.0.1:8420/",                       // the board
    "http://127.0.0.1:8420/token.html",             // board page named alike
    "https://evil.example/token.html",
    pathToFileURL(path.join("/apps", "no_human", "error.html")).href,
    pathToFileURL(path.join("/elsewhere", "token.html")).href,
    "file://../token.html",                         // traversal attempt
    // Strict-prefix lookalikes. Without these, relaxing the check to
    // startsWith() passes the whole suite, and any board-writable file whose
    // path merely BEGINS with token.html could drive the credential IPC.
    pathToFileURL(`${SETUP}.evil`).href,
    pathToFileURL(`${SETUP}x`).href,
    pathToFileURL(path.join(SETUP, "nested.html")).href,
  ]) {
    assert.equal(isSetupUrl(url, SETUP), false, `must reject ${url}`);
  }
});

test("isSetupUrl: fails closed on missing or malformed input", () => {
  for (const url of ["", null, undefined, 0, {}, "not a url", "file://"]) {
    assert.equal(isSetupUrl(url, SETUP), false, `must reject ${String(url)}`);
  }
});
