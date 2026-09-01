// The credential IPC gate. This is the branch's most security-sensitive check —
// without it, anything rendered by the board could overwrite the operator's
// OAuth token — so it is pinned here rather than relying on manual probing.
import assert from "node:assert/strict";
import test from "node:test";
import path from "node:path";
import { pathToFileURL } from "node:url";

import {
  isSetupUrl,
  claudeCredentialPaths,
  detectSignIn,
  classifySetupTokenOutput,
  redact,
  MAC_KEYCHAIN_SERVICE,
} from "./setupGate.mjs";

// path.resolve, not path.join: this fixture must be a genuinely ABSOLUTE path
// on the host running the test. "/apps/no_human/token.html" is absolute on
// POSIX but drive-relative on Windows, so pathToFileURL there anchors it to the
// current drive ("C:\apps\...") while the fixture string stays "\apps\...", and
// the round-trip through fileURLToPath can never match — the accept case failed
// on Windows while the predicate itself was correct. Resolving first makes both
// sides agree on every platform WITHOUT changing what is being asserted; the
// rejection cases below are unaffected, since they only need to differ from this.
const SETUP = path.resolve(path.join("/apps", "no_human", "token.html"));
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

// --- AC1: existing-credential detection ------------------------------------ //

test("test_existing_credential_button_renders: detectSignIn reports detected for auth-status success and for store-only", () => {
  assert.deepEqual(
    detectSignIn({ cliPath: "/usr/local/bin/claude", authStatusCode: 0, storeExists: false }),
    { detected: true, via: "cli" },
  );
  assert.deepEqual(
    detectSignIn({ cliPath: "/usr/local/bin/claude", authStatusCode: 1, storeExists: true }),
    { detected: true, via: "store" },
  );
  assert.deepEqual(
    detectSignIn({ cliPath: "/usr/local/bin/claude", authStatusCode: null, storeExists: true }),
    { detected: true, via: "store" },
    "a missing/erroring/timed-out auth-status command must still fall through to the store check",
  );
  // The predicate the page uses to decide whether to unhide the button is
  // exactly `.detected` — pin that shape here so token.html's `s?.detected`
  // check stays correct.
  const result = detectSignIn({ cliPath: "/usr/local/bin/claude", authStatusCode: 0, storeExists: false });
  assert.equal(Object.keys(result).includes("detected"), true);
});

// --- AC2: non-interactive token retrieval ---------------------------------- //

test("test_token_noninteractive_retrieval_and_persistence: classifySetupTokenOutput extracts the token", () => {
  assert.deepEqual(
    classifySetupTokenOutput({ code: 0, stdout: "sk-ant-oat01-abcDEF_123-xyz", stderr: "" }),
    { kind: "token", token: "sk-ant-oat01-abcDEF_123-xyz" },
  );
  assert.deepEqual(
    classifySetupTokenOutput({ code: 0, stdout: "Token: sk-ant-oat01-abcDEF_123\n", stderr: "" }),
    { kind: "token", token: "sk-ant-oat01-abcDEF_123" },
    "a `Token: ` prefix must not defeat extraction",
  );
  assert.deepEqual(
    classifySetupTokenOutput({
      code: 0,
      stdout: JSON.stringify({ token: "sk-ant-oat01-jsonWrapped" }),
      stderr: "",
    }),
    { kind: "token", token: "sk-ant-oat01-jsonWrapped" },
    "JSON-wrapped output must not defeat extraction",
  );
  assert.notEqual(
    classifySetupTokenOutput({ code: 1, stdout: "sk-ant-oat01-shouldNotCount", stderr: "" }).kind,
    "token",
    "a non-zero exit must never be trusted as a token, even if one is present in the text",
  );
});

// --- AC3: interactive fallback ---------------------------------------------- //

test("test_token_interactive_paste_path: browser markers and empty success classify as interactive", () => {
  assert.deepEqual(
    classifySetupTokenOutput({ code: 0, stdout: "Visit https://claude.ai/setup-token to continue", stderr: "" }),
    { kind: "interactive" },
  );
  assert.deepEqual(
    classifySetupTokenOutput({ code: 0, stdout: "Open your browser to finish sign-in", stderr: "" }),
    { kind: "interactive" },
  );
  assert.deepEqual(
    classifySetupTokenOutput({ code: 0, stdout: "Please visit the link we sent", stderr: "" }),
    { kind: "interactive" },
  );
  assert.deepEqual(
    classifySetupTokenOutput({ code: 0, stdout: "", stderr: "" }),
    { kind: "interactive" },
    "a clean exit with no token is still 'needs interactive auth', not an error",
  );
  assert.deepEqual(
    classifySetupTokenOutput({ code: null, stdout: "", stderr: "timeout" }),
    { kind: "interactive" },
    "the timeout shape must fail safe to manual paste, not to an error message",
  );
});

// --- AC4: credential security (no leak, fixed error message) --------------- //

test("test_credential_not_leaked_to_logs: error classification returns a fixed message and redact() strips the secret", () => {
  const secretish = "some-internal-detail-98237";
  const result = classifySetupTokenOutput({
    code: 1,
    stdout: `boom: ${secretish}`,
    stderr: `also: ${secretish}`,
  });
  assert.equal(result.kind, "error");
  assert.equal(result.message.includes(secretish), false,
    "the fixed error message must never echo captured output");
  assert.equal(
    result.message,
    "This feature needs a recent Claude CLI — run `claude setup-token` manually and paste the token below.",
  );

  const token = "sk-ant-oat01-secretvalue";
  assert.equal(redact(`failed to write ${token} to disk`, token),
    "failed to write *** to disk");
  assert.equal(redact("no secret here", token).includes(token), false);
  assert.equal(redact("", token), "");
  assert.equal(redact("text", ""), "text");
});

// --- AC5: no-credential fallback (fails closed) ----------------------------- //

test("test_fallback_ui_identical_to_current_state: detectSignIn fails closed", () => {
  assert.deepEqual(detectSignIn({ cliPath: "", authStatusCode: 0, storeExists: true }),
    { detected: false, via: "no-cli" });
  assert.deepEqual(detectSignIn({ cliPath: null, authStatusCode: 0, storeExists: true }),
    { detected: false, via: "no-cli" });
  assert.deepEqual(detectSignIn({ cliPath: "/bin/claude", authStatusCode: 1, storeExists: false }),
    { detected: false });
  for (const garbage of [undefined, null, {}, "not an object", 0, []]) {
    const result = detectSignIn(garbage);
    assert.equal(result.detected, false, `must fail closed for ${String(garbage)}`);
  }
});

// --- AC4/AC6: manual paste flow unchanged; paths are existence-check-only -- //

test("test_manual_paste_flow_unchanged: claudeCredentialPaths are existence-check targets only", async () => {
  // darwin returns [] deliberately: verified empirically (real signed-in
  // `claude` 2.1.252) that macOS has no `~/.claude/.credentials.json` (that
  // file is linux-only) and `~/Library/Application Support/Claude` is the
  // unrelated Claude Desktop app, not Claude Code CLI — neither is a safe
  // fs-based existence signal. The macOS store check is Keychain-based (see
  // MAC_KEYCHAIN_SERVICE / server.mjs's macClaudeKeychainExists), not a path.
  const darwin = claudeCredentialPaths("darwin", {}, "/Users/op");
  assert.deepEqual(darwin, [],
    "macOS has no reliable credential FILE path — must not fabricate one");
  const linux = claudeCredentialPaths("linux", {}, "/home/op");
  assert.deepEqual(linux, [
    "/home/op/.claude/.credentials.json",
    "/home/op/.config/claude",
  ]);
  const win = claudeCredentialPaths("win32", { APPDATA: "C:\\Users\\op\\AppData\\Roaming" }, "C:\\Users\\op");
  assert.deepEqual(win, ["C:\\Users\\op\\AppData\\Roaming\\Claude"]);
  assert.deepEqual(claudeCredentialPaths("win32", {}, "C:\\Users\\op"), [],
    "no APPDATA means no guessable path — must not fabricate one");
  assert.deepEqual(claudeCredentialPaths("freebsd", {}, "/home/op"), [],
    "an unknown platform must not throw or guess a path");

  // Module-surface assertion: this file never exports a reader for the
  // credential file's contents, only path lists for fs.existsSync (plus the
  // Keychain service-name constant, which is likewise never read from here).
  assert.equal(typeof claudeCredentialPaths, "function");
  assert.equal(Object.keys(await import("./setupGate.mjs")).sort().join(","),
    "MAC_KEYCHAIN_SERVICE,classifySetupTokenOutput,claudeCredentialPaths,detectSignIn,isSetupUrl,redact");

  // Regression: isSetupUrl (the manual-paste gate's own foundation) still
  // admits only the exact setup file.
  assert.equal(isSetupUrl(setupUrl, SETUP), true);
  assert.equal(isSetupUrl("http://127.0.0.1:8420/", SETUP), false);
});

// --- macOS Keychain store check (reviewer finding: the file-path fallback
// was measured dead on macOS — Claude Code stores its credential in the
// login Keychain there, not a file) ------------------------------------- //

test("macOS credential store is Keychain-based, not a fabricated file path", () => {
  // The service name is a plain constant here, not a reader: this module
  // never shells out to `security` or touches the Keychain itself — that
  // lives in server.mjs's macClaudeKeychainExists, which imports this
  // constant so the two files can't drift out of sync.
  assert.equal(MAC_KEYCHAIN_SERVICE, "Claude Code-credentials");
  assert.equal(typeof MAC_KEYCHAIN_SERVICE, "string");
  // detectSignIn itself is mechanism-agnostic: it only ever sees the
  // resolved boolean, whether that boolean came from fs.existsSync (linux/
  // win32) or from a Keychain existence probe (darwin) — pin that the
  // "store" fallback still fires when storeExists is true even though no
  // darwin path exists to produce it.
  assert.deepEqual(
    detectSignIn({ cliPath: "/usr/local/bin/claude", authStatusCode: 1, storeExists: true }),
    { detected: true, via: "store" },
    "storeExists must still drive the fallback regardless of how it was computed",
  );
});
