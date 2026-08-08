// Unit tests for credential storage. Pure logic over a real temp HOME — no
// mocks, and never the operator's real ~/.no_human/.env.
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { execFileSync } from "node:child_process";

import {
  CredentialPermissionError,
  TOKEN_KEY, configuredProfile, envPath, hasToken, icaclsGrantees,
  isWindowsTcbPrincipal, nonOwnerGrantees, parseEnv,
  tokenVarFor, validateToken, windowsOwnerPrincipal, writeToken,
} from "./tokenStore.mjs";

/**
 * Run *fn* with the platform's "restrict this file to its owner" primitive
 * forced to fail, then restore it.
 *
 * Stubs `fs.chmodSync` rather than `restrictToOwner` itself, so the REAL
 * restrictToOwner runs and throws from its real POSIX branch — the same shape
 * `windowsRestrictToOwner` throws when icacls is missing, exits non-zero, or
 * reads back an ACL that still lists SYSTEM. `import fs from "node:fs"` yields
 * node:fs's own module.exports object in both this file and tokenStore.mjs, so
 * the assignment is visible to the code under test; the `assert.throws` in each
 * caller is what proves the stub actually took effect rather than passing
 * vacuously.
 */
function withFailingRestrict(fn) {
  const real = fs.chmodSync;
  fs.chmodSync = () => {
    throw new CredentialPermissionError("forced failure: cannot secure the file");
  };
  try {
    return fn();
  } finally {
    fs.chmodSync = real;
  }
}

const home = () => mkdtempSync(join(tmpdir(), "nhhome-"));

// withFailingRestrict forces the POSIX primitive (fs.chmodSync) to throw so the
// REAL restrictToOwner fails closed. On a real Windows host restrictToOwner
// dispatches to icacls instead, which has NO mockable seam from a test: it is a
// named `execFileSync` import (not the reassignable `fs` module object the chmod
// stub relies on) and the real binary lives in System32, so emptying PATH cannot
// hide it either. The forced-failure ORDERING guard is therefore only observable
// on POSIX, where it runs in full. The Windows END STATE — a correctly restricted
// .env — is covered by "writeToken: creates the file 0600", which reads the real
// ACL back. (Coverage gap on real Windows: the writeToken cleanup-on-restrict-
// failure path is unexercised; closing it needs a mockable icacls seam.)
const SKIP_FAILING_RESTRICT = process.platform === "win32"
  ? "forcing restrictToOwner to fail needs the POSIX chmod seam; on Windows it "
    + "uses icacls, which has no mockable seam (named import; real binary in "
    + "System32). POSIX runs this in full; the Windows end state is covered by "
    + "\"writeToken: creates the file 0600\"."
  : false;

/**
 * Assert the platform's "only the owner can read this" guarantee.
 *
 * The assertion used to be `statSync(p).mode & 0o777 === 0o600` everywhere.
 * That can NEVER hold on Windows — node reports 0o666 for any file without the
 * readonly attribute, whatever the ACL says — so on Windows it was testing a
 * number the OS does not model rather than the property anyone cares about.
 *
 * So the PROPERTY is asserted per platform, and the Windows form reads the real
 * ACL back. The owner must be present, and NO principal outside the platform
 * TCB (SYSTEM + the local Administrators group — POSIX root's analog, which the
 * POSIX branch's 0600 also cannot exclude) may appear. That still catches the
 * original defect — a .env inherited by *other* accounts — while accepting the
 * TCB, which is unavoidable on an admin-owned file (the CI runner's case).
 */
function assertOwnerOnly(p) {
  if (process.platform !== "win32") {
    assert.equal(fs.statSync(p).mode & 0o777, 0o600);
    return;
  }
  const out = execFileSync("icacls", [p], { encoding: "utf8", windowsHide: true });
  const grantees = icaclsGrantees(out, p);
  const owner = windowsOwnerPrincipal();
  const got = [...grantees].map((g) => g.toLowerCase()).sort();
  assert.ok(got.includes(owner.toLowerCase()),
    `${p} must grant its owner, got: ${got.join(", ")}`);
  const extra = nonOwnerGrantees(grantees, owner);
  assert.deepEqual(extra, [],
    `${p} must grant only its owner and the platform TCB, but also grants: `
    + extra.join(", "));
}

// The readback filter, tested directly (the real icacls path is Windows-only).
// SYSTEM and the local Administrators group are the platform TCB and accepted;
// any OTHER non-owner account is the fail-closed signal that protects a
// non-admin user's credential.
test("nonOwnerGrantees: accepts owner + TCB, flags any other account", () => {
  const readback =
    "C:\\Users\\alice\\.no_human\\.env CORP\\alice:(R,W)\n"
    + "        NT AUTHORITY\\SYSTEM:(F)\n"
    + "        BUILTIN\\Administrators:(F)\n"
    + "        BUILTIN\\Users:(RX)\n"
    + "\nSuccessfully processed 1 files; Failed processing 0 files\n";
  const grantees = icaclsGrantees(readback, "C:\\Users\\alice\\.no_human\\.env");
  assert.deepEqual([...grantees].sort(), [
    "BUILTIN\\Administrators", "BUILTIN\\Users",
    "CORP\\alice", "NT AUTHORITY\\SYSTEM",
  ]);
  // SYSTEM and Administrators are accepted (owner too); only Users survives.
  assert.deepEqual(nonOwnerGrantees(grantees, "CORP\\alice"), ["BUILTIN\\Users"]);
  // Owner + TCB alone ⇒ nothing flagged.
  assert.deepEqual(
    nonOwnerGrantees(
      new Set(["CORP\\alice", "NT AUTHORITY\\SYSTEM", "BUILTIN\\Administrators"]),
      "CORP\\alice"),
    []);
});

test("isWindowsTcbPrincipal: names and well-known SIDs, not other accounts", () => {
  for (const g of ["NT AUTHORITY\\SYSTEM", "builtin\\administrators",
                   "S-1-5-18", "*S-1-5-32-544"]) {
    assert.ok(isWindowsTcbPrincipal(g), `${g} is the platform TCB`);
  }
  for (const g of ["BUILTIN\\Users", "Everyone", "CORP\\bob", "S-1-5-11"]) {
    assert.ok(!isWindowsTcbPrincipal(g), `${g} must NOT be accepted`);
  }
});

test("hasToken: the .env file wins, then process env, else false", () => {
  const h = home();
  assert.equal(hasToken({}, h), false, "empty home has no token");
  assert.equal(hasToken({ [TOKEN_KEY]: "sk-ant-oat-x" }, h), true);
  assert.equal(hasToken({ [TOKEN_KEY]: "   " }, h), false, "blank is not a token");
  writeToken("sk-ant-oat-fromfile", h);
  assert.equal(hasToken({}, h), true, "reads the .env file");
});

test("writeToken: preserves other secrets, one token line after rewrite", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(envPath(h),
    "# comment\nJIRA_API_TOKEN=keepme\nCIRCLECI_TOKEN=keeptoo\n");
  writeToken("sk-ant-oat-first", h);
  let text = fs.readFileSync(envPath(h), "utf8");
  assert.match(text, /JIRA_API_TOKEN=keepme/, "other secrets survive");
  assert.match(text, /CIRCLECI_TOKEN=keeptoo/);
  assert.match(text, /# comment/, "comments survive");
  assert.equal(parseEnv(text)[TOKEN_KEY], "sk-ant-oat-first");

  // Rewriting must REPLACE, not append a second line.
  writeToken("sk-ant-oat-second", h);
  text = fs.readFileSync(envPath(h), "utf8");
  assert.equal(parseEnv(text)[TOKEN_KEY], "sk-ant-oat-second");
  const occurrences = text.split("\n").filter((l) => l.startsWith(TOKEN_KEY));
  assert.equal(occurrences.length, 1, "exactly one token line");
  assert.match(text, /JIRA_API_TOKEN=keepme/, "still preserved on rewrite");
});

test("writeToken: creates the file 0600", () => {
  const h = home();
  const p = writeToken("sk-ant-oat-x", h);
  assertOwnerOnly(p);
});

test("writeToken: removes DUPLICATE keys — dotenv is later-wins", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(envPath(h),
    `${TOKEN_KEY}=OLD1\nFOO=b\n${TOKEN_KEY}=OLD2\n`);
  writeToken("sk-ant-oat-new", h);
  const text = fs.readFileSync(envPath(h), "utf8");
  assert.equal(parseEnv(text)[TOKEN_KEY], "sk-ant-oat-new",
    "the effective (last) value must be the new one");
  assert.equal(text.split("\n").filter((l) => l.startsWith(TOKEN_KEY)).length, 1);
  assert.match(text, /FOO=b/);
});

test("writeToken: hardens an existing 0644 .env to 0600", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(envPath(h), "FOO=b\n", { mode: 0o644 });
  fs.chmodSync(envPath(h), 0o644);
  writeToken("sk-ant-oat-x", h);
  assertOwnerOnly(envPath(h));
});

// The ordering guard. Before the atomic-rename fix these two passed ONLY
// because nothing asserted them: the credential was written to .env first and
// restrictToOwner ran after, so a failure left a readable token on disk and
// nh:save-token reported {ok:false} over it.
test("writeToken: a failing restrictToOwner leaves NO .env behind",
  { skip: SKIP_FAILING_RESTRICT }, () => {
  const h = home();
  withFailingRestrict(() => {
    assert.throws(() => writeToken("sk-ant-oat-mustnotpersist", h),
      /forced failure: cannot secure the file/);
  });
  assert.equal(fs.existsSync(envPath(h)), false,
    ".env must not exist: the credential may not reach disk before its "
    + "permissions are proven");
  assert.equal(fs.existsSync(`${envPath(h)}.tmp`), false,
    "the temp file must be cleaned up, not left holding the credential");
});

test("writeToken: a failing restrictToOwner leaves an EXISTING .env untouched",
  { skip: SKIP_FAILING_RESTRICT }, () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  const before = "# comment\nJIRA_API_TOKEN=keepme\n";
  fs.writeFileSync(envPath(h), before);
  withFailingRestrict(() => {
    assert.throws(() => writeToken("sk-ant-oat-mustnotpersist", h));
  });
  const after = fs.readFileSync(envPath(h), "utf8");
  assert.equal(after, before, "an existing .env must be byte-identical");
  assert.ok(!after.includes("sk-ant-oat-mustnotpersist"),
    "no credential byte may reach disk on the failure path");
  assert.equal(fs.existsSync(`${envPath(h)}.tmp`), false,
    "the temp file must be cleaned up");
});

test("writeToken: always ends with a newline", () => {
  const h = home();
  const p = writeToken("sk-ant-oat-x", h);
  assert.ok(fs.readFileSync(p, "utf8").endsWith("\n"));
});

test("profile-aware: a named auth_profile uses its own key", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(join(h, ".no_human", "config.yaml"),
    "llm:\n  auth_profile: personal\n");
  assert.equal(configuredProfile(h), "personal");
  assert.equal(tokenVarFor("personal"), `${TOKEN_KEY}_PERSONAL`);
  writeToken("sk-ant-oat-profile", h);
  const env = parseEnv(fs.readFileSync(envPath(h), "utf8"));
  assert.equal(env[`${TOKEN_KEY}_PERSONAL`], "sk-ant-oat-profile");
  assert.equal(env[TOKEN_KEY], undefined, "must not write the bare key");
  assert.equal(hasToken({}, h), true);
});

test("hasToken: a bare token does NOT satisfy a named profile", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(join(h, ".no_human", "config.yaml"),
    "llm:\n  auth_profile: personal\n");
  fs.writeFileSync(envPath(h), `${TOKEN_KEY}=bare-only\n`);
  assert.equal(hasToken({}, h), false);
});

test("validateToken: rejects empty, whitespace and API keys", () => {
  assert.notEqual(validateToken(""), "");
  assert.notEqual(validateToken("   "), "");
  assert.match(validateToken("sk-ant-api03-abc"), /API key/);
  assert.match(validateToken("SK-ANT-API03-ABC"), /API key/, "case-insensitive");
  assert.notEqual(validateToken("has space"), "");
  assert.equal(validateToken("sk-ant-oat-valid"), "", "a real token passes");
});

test("writeToken: refuses to persist an invalid value", () => {
  const h = home();
  assert.throws(() => writeToken("", h));
  assert.throws(() => writeToken("sk-ant-api03-nope", h));
  assert.equal(fs.existsSync(envPath(h)), false, "nothing written on reject");
});

// ── E2: BYO API key — the sanctioned opt-in billing mode ────────────────────
// The desktop screen historically accepted ONLY a subscription token; the
// backend has supported llm.auth_mode: "api_key" since 2026-07-24. These pin
// the desktop half: mode-aware validation, storage, config upsert, and gate.

import {
  API_KEY_VAR, configuredAuthMode, hasCredential, setAuthMode,
  validateCredential, writeCredential,
} from "./tokenStore.mjs";

test("validateCredential: api_key mode requires the sk-ant-api shape", () => {
  assert.equal(validateCredential("sk-ant-api03-abc", "api_key"), "");
  assert.equal(validateCredential("SK-ANT-API03-ABC", "api_key"), "");
  assert.notEqual(validateCredential("", "api_key"), "");
  assert.notEqual(validateCredential("has space", "api_key"), "");
  assert.match(validateCredential("sk-ant-oat-tok", "api_key"), /subscription token/i,
    "an OAuth token pasted into the key field gets a pointer, not a shrug");
  assert.notEqual(validateCredential("hello", "api_key"), "");
});

test("validateCredential: subscription mode keeps today's rules exactly", () => {
  assert.equal(validateCredential("sk-ant-oat-valid", "subscription"), "");
  assert.match(validateCredential("sk-ant-api03-abc", "subscription"), /API key/);
  assert.notEqual(validateCredential("", "subscription"), "");
});

test("writeCredential: api_key mode writes ANTHROPIC_API_KEY, 0600, others preserved", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(envPath(h), "JIRA_API_TOKEN=keepme\n");
  const p = writeCredential("sk-ant-api03-abc", "api_key", h);
  const text = fs.readFileSync(p, "utf8");
  assert.equal(parseEnv(text)[API_KEY_VAR], "sk-ant-api03-abc");
  assert.match(text, /JIRA_API_TOKEN=keepme/);
  assertOwnerOnly(p);
  assert.throws(() => writeCredential("sk-ant-oat-x", "api_key", h),
    /subscription token/i, "wrong shape never touches disk");
});

test("writeCredential: subscription mode behaves exactly like writeToken", () => {
  const h = home();
  writeCredential("sk-ant-oat-x", "subscription", h);
  assert.equal(parseEnv(fs.readFileSync(envPath(h), "utf8"))[TOKEN_KEY], "sk-ant-oat-x");
});

test("setAuthMode: creates config.yaml with an llm block when absent", () => {
  const h = home();
  setAuthMode("api_key", h);
  const text = fs.readFileSync(join(h, ".no_human", "config.yaml"), "utf8");
  assert.match(text, /^llm:$/m);
  assert.match(text, /^  auth_mode: api_key$/m);
  assert.equal(configuredAuthMode(h), "api_key");
});

test("setAuthMode: upserts inside an existing llm block, preserving neighbours", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(join(h, ".no_human", "config.yaml"),
    "server:\n  port: 8420\nllm:\n  auth_profile: personal\n  auth_mode: subscription\ngit:\n  x: y\n");
  setAuthMode("api_key", h);
  const text = fs.readFileSync(join(h, ".no_human", "config.yaml"), "utf8");
  assert.match(text, /^  auth_mode: api_key$/m);
  assert.doesNotMatch(text, /auth_mode: subscription/);
  assert.match(text, /auth_profile: personal/, "sibling keys survive");
  assert.match(text, /port: 8420/, "other blocks survive");
  assert.match(text, /^git:$/m);
  // Switching BACK must work too (nh init does this in both directions).
  setAuthMode("subscription", h);
  assert.equal(configuredAuthMode(h), "subscription");
});

test("setAuthMode: adds auth_mode to an llm block that lacks one", () => {
  const h = home();
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(join(h, ".no_human", "config.yaml"),
    "llm:\n  auth_profile: personal\n");
  setAuthMode("api_key", h);
  assert.equal(configuredAuthMode(h), "api_key");
  assert.match(fs.readFileSync(join(h, ".no_human", "config.yaml"), "utf8"),
    /auth_profile: personal/);
});

test("setAuthMode: rejects an unknown mode without touching the file", () => {
  const h = home();
  assert.throws(() => setAuthMode("bedrock", h));
  assert.equal(fs.existsSync(join(h, ".no_human", "config.yaml")), false);
});

test("hasCredential: follows the configured mode", () => {
  const h = home();
  // Default (no config) = subscription: an API key alone does not satisfy it.
  fs.mkdirSync(join(h, ".no_human"), { recursive: true });
  fs.writeFileSync(envPath(h), `${API_KEY_VAR}=sk-ant-api03-abc\n`);
  assert.equal(hasCredential({}, h), false,
    "subscription mode must not be satisfied by an API key");
  // api_key mode: the key satisfies it, .env wins, process env is the fallback.
  setAuthMode("api_key", h);
  assert.equal(hasCredential({}, h), true);
  fs.writeFileSync(envPath(h), "");
  assert.equal(hasCredential({}, h), false);
  assert.equal(hasCredential({ [API_KEY_VAR]: "sk-ant-api03-env" }, h), true);
  // And a subscription token does not satisfy api_key mode.
  writeToken("sk-ant-oat-x", h);
  assert.equal(hasCredential({}, h), false,
    "api_key mode must not be satisfied by an OAuth token");
});
