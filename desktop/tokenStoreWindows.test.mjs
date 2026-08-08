// The credential file's ORDERING, and the Windows branch that had never run.
//
// WHAT THIS PINS, and why the existing 21 tests in tokenStore.test.mjs do not.
// Those assert the END STATE of ~/.no_human/.env: after writeToken returns, the
// file is 0600 (or owner-only by ACL). End state is exactly what survives the
// defect this file exists to prevent. Move the write INSIDE the temp block —
//     fs.closeSync(fs.openSync(tmp, "w", 0o600));
//     fs.writeFileSync(tmp, kept.join("\n"));   // credential on disk, inherited ACL
//     restrictToOwner(tmp);
//     fs.renameSync(tmp, p);
// — and every one of those 21 still passes, because the end state is identical.
// What changed is the WINDOW: on Windows the token now sits in `.env.tmp` under
// the inherited SYSTEM/Administrators ACL across two execFileSync spawns.
// writeEnvVar's own comment calls this out — "ORDERING IS THE WHOLE PROPERTY" —
// and ordering was the one thing nothing asserted. So the assertions here are
// about WHEN, not what: the temp file must be ZERO BYTES at the instant the
// platform's restrict primitive is invoked on it.
//
// This mirrors the Python half, which already had the guard the Electron half
// lacked: tests/test_windows_credential_file.py's
// `test_atomic_write_0600_windows_restricts_acl_before_writing`, whose docstring
// says "the grant happens while the file is still EMPTY". The two halves write
// the SAME file and must not disagree about when it becomes private.
//
// AND: until this file existed, `windowsRestrictToOwner` had never executed on
// any machine, in any suite. `restrictToOwner`'s dispatch read a module-load
// `const IS_WINDOWS`, unpatchable from a test, and there is no Windows CI — so
// the icacls command shaping, the ACL readback, all four of its throw sites and
// runIcacls' missing-binary refusal were dead code everywhere we can observe. They are exercised below against a fake
// `icacls` placed on PATH, which means the REAL execFileSync, the REAL argv
// shaping and the REAL icaclsGrantees parsing all run; only the binary is ours.
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  CredentialPermissionError,
  restrictToOwner, windowsOwnerPrincipal, windowsRestrictToOwner, writeToken,
} from "./tokenStore.mjs";

// Captured before anything can flip it, and restored by descriptor rather than
// by value so `process.platform` is left exactly as node defined it.
const REAL_PLATFORM = process.platform;
const REAL_PLATFORM_DESC = Object.getOwnPropertyDescriptor(process, "platform");

/**
 * Run *fn* with the platform reading as Windows, then put it back.
 *
 * CONTAINMENT, three layers, because this is a security-relevant dispatch:
 *  1. `node --test` gives every test FILE its own process, so a flip cannot
 *     reach another file even if this one crashed mid-test.
 *  2. `finally` restores the original property DESCRIPTOR, not a copied value.
 *  3. The last test in this file asserts the platform really is back AND that
 *     restrictToOwner takes the POSIX path — a leak is caught, not assumed.
 */
function asWindows(fn) {
  Object.defineProperty(process, "platform", { value: "win32", configurable: true });
  try {
    return fn();
  } finally {
    Object.defineProperty(process, "platform", REAL_PLATFORM_DESC);
  }
}

const home = () => fs.mkdtempSync(path.join(os.tmpdir(), "nhwin-"));

/**
 * A fake `icacls` on PATH.
 *
 * Faking the BINARY rather than execFileSync keeps the module under test
 * unmodified: windowsRestrictToOwner builds its own argv, spawns it through the
 * real execFileSync, and parses the real stdout back. Only what the OS would
 * have run is ours. Behaviour is driven by env vars so one script covers the
 * happy path and every failure the throw sites are written for.
 *
 * Not usable on a real Windows box (it is a /bin/sh script), which is why every
 * test using it is skipped when the HOST platform is genuinely win32 — there
 * the branch is reachable natively and the real icacls is the better oracle.
 */
function fakeIcacls({ readBody, grantRc, readRc } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "nhbin-"));
  const log = path.join(dir, "icacls.log");
  fs.writeFileSync(log, "");
  fs.writeFileSync(path.join(dir, "icacls"), `#!/bin/sh
log="$NH_FAKE_LOG"
if [ $# -eq 1 ]; then
  printf 'READ\\n' >> "$log"
  printf 'ARG %s\\n' "$1" >> "$log"
  if [ -n "$NH_FAKE_READ_BODY" ]; then printf '%s\\n' "$NH_FAKE_READ_BODY"; fi
  echo "Successfully processed 1 files; Failed processing 0 files"
  exit \${NH_FAKE_READ_RC:-0}
fi
printf 'GRANT\\n' >> "$log"
for a in "$@"; do printf 'ARG %s\\n' "$a" >> "$log"; done
printf 'GRANTSIZE %s\\n' "$(wc -c < "$1" | tr -d ' ')" >> "$log"
echo "Successfully processed 1 files; Failed processing 0 files"
exit \${NH_FAKE_GRANT_RC:-0}
`, { mode: 0o755 });

  const saved = {
    PATH: process.env.PATH, USERNAME: process.env.USERNAME,
    USERDOMAIN: process.env.USERDOMAIN, NH_FAKE_LOG: process.env.NH_FAKE_LOG,
    NH_FAKE_READ_BODY: process.env.NH_FAKE_READ_BODY,
    NH_FAKE_GRANT_RC: process.env.NH_FAKE_GRANT_RC,
    NH_FAKE_READ_RC: process.env.NH_FAKE_READ_RC,
  };
  process.env.PATH = `${dir}:${process.env.PATH}`;
  process.env.USERNAME = "tessa";
  delete process.env.USERDOMAIN;
  process.env.NH_FAKE_LOG = log;
  if (readBody !== undefined) process.env.NH_FAKE_READ_BODY = readBody;
  else delete process.env.NH_FAKE_READ_BODY;
  if (grantRc !== undefined) process.env.NH_FAKE_GRANT_RC = String(grantRc);
  else delete process.env.NH_FAKE_GRANT_RC;
  if (readRc !== undefined) process.env.NH_FAKE_READ_RC = String(readRc);
  else delete process.env.NH_FAKE_READ_RC;

  return {
    dir,
    /** The fake's transcript, one token per line. */
    log: () => fs.readFileSync(log, "utf8"),
    args: () => fs.readFileSync(log, "utf8").split("\n")
      .filter((l) => l.startsWith("ARG ")).map((l) => l.slice(4)),
    restore() {
      for (const [k, v] of Object.entries(saved)) {
        if (v === undefined) delete process.env[k];
        else process.env[k] = v;
      }
      fs.rmSync(dir, { recursive: true, force: true });
    },
  };
}

/** A readback line in icacls' real shape: `<path> <PRINCIPAL>:(perms)`. */
const aclLine = (p, principal) => `${p} ${principal}:(R,W)`;

// A /bin/sh fake cannot stand in for icacls.exe on a real Windows host.
const skipOnRealWindows = { skip: REAL_PLATFORM === "win32"
  ? "the fake icacls is a POSIX shell script; on a real Windows host the branch "
    + "is reachable natively and the real icacls is the better oracle"
  : false };

// ---------------------------------------------------------------------------
// THE ORDERING PROPERTY
// ---------------------------------------------------------------------------

test("POSIX: the temp file is EMPTY when it is chmodded, not after the write",
  () => {
    // Runs on the platform we actually develop and CI on, so the ordering stays
    // pinned even for someone who deletes every win32 test in this file.
    // Wrapping fs.chmodSync is the mechanism tokenStore.test.mjs already
    // documents and uses: `import fs from "node:fs"` yields node:fs's own
    // module.exports object in both files, so the assignment is visible to the
    // code under test.
    const h = home();
    const real = fs.chmodSync;
    const seen = [];
    fs.chmodSync = (p, mode) => {
      seen.push({ path: String(p), mode, size: fs.statSync(p).size });
      return real(p, mode);
    };
    try {
      writeToken("sk-ant-oat-ordering", h);
    } finally {
      fs.chmodSync = real;
    }

    assert.ok(seen.length >= 1,
      "restrictToOwner never chmodded anything — this test is vacuous and the "
      + "credential file's permissions are set by nothing");
    const first = seen[0];
    assert.ok(first.path.endsWith(".env.tmp"),
      `the first restrict landed on ${first.path}, not the sibling temp file. `
      + "Hardening the real .env in place is the defect: it is hardened only "
      + "after the credential is already in it");
    assert.equal(first.mode, 0o600,
      "the temp file must be restricted to its owner, not merely chmodded");
    assert.equal(first.size, 0,
      "THE ORDERING DEFECT: the temp file already held "
      + `${first.size} byte(s) when it was restricted, so the credential was `
      + "written to disk under the inherited permissions first. On Windows "
      + "chmod is a no-op and those bytes stay world-readable across two "
      + "icacls spawns. Restrict the EMPTY temp file, then write it");
  });

test("Windows: icacls grants on the EMPTY temp file, before the credential lands",
  skipOnRealWindows, () => {
    // The same property on the platform it actually protects, through the real
    // dispatch: restrictToOwner -> windowsRestrictToOwner -> execFileSync.
    const h = home();
    const fake = fakeIcacls({ readBody: null });
    try {
      // readBody must name the temp path, which is only known inside the run;
      // a matching principal keeps the happy path happy.
      process.env.NH_FAKE_READ_BODY =
        aclLine(path.join(h, ".no_human", ".env.tmp"), "tessa");
      asWindows(() => writeToken("sk-ant-oat-winordering", h));

      const log = fake.log();
      assert.match(log, /^GRANT$/m,
        "no icacls grant ran at all — the win32 dispatch was not taken and "
        + "this test is vacuous");
      const size = Number(log.match(/^GRANTSIZE (\d+)$/m)?.[1]);
      assert.equal(size, 0,
        `THE ORDERING DEFECT: icacls was handed a temp file already holding `
        + `${size} byte(s). Those bytes are the OAuth token, on disk under the `
        + "inherited SYSTEM/Administrators ACL, for the whole duration of the "
        + "grant and the readback. Restrict the EMPTY temp file, then write it");
      const target = fake.args()[0];
      assert.ok(target.endsWith(".env.tmp"),
        `icacls was pointed at ${target}, not the sibling temp file`);
    } finally {
      fake.restore();
    }
  });

// ---------------------------------------------------------------------------
// THE WINDOWS BRANCH ITSELF — shaping, readback, and every throw site
// ---------------------------------------------------------------------------

test("windowsRestrictToOwner shapes the icacls argv and READS THE ACL BACK",
  skipOnRealWindows, () => {
    const f = path.join(home(), "creds");
    fs.writeFileSync(f, "");
    const fake = fakeIcacls({ readBody: aclLine(f, "tessa") });
    try {
      windowsRestrictToOwner(f);
      const log = fake.log();
      // Two spawns, in this order: replace the ACL, then verify it.
      assert.deepEqual(
        log.split("\n").filter((l) => l === "GRANT" || l === "READ"),
        ["GRANT", "READ"],
        "the ACL must be replaced and then READ BACK. A command that reported "
        + "success having changed nothing is the exact defect this replaces");
      assert.deepEqual(fake.args().slice(0, 4),
        [f, "/inheritance:r", "/grant:r", "tessa:(R,W)"],
        "the grant argv changed. /inheritance:r is what drops the inherited "
        + "SYSTEM and Administrators ACEs; without it /grant:r only ADDS an "
        + "entry and the file stays readable by both");
    } finally {
      fake.restore();
    }
  });

test("windowsRestrictToOwner uses inheritable flags for a DIRECTORY only",
  skipOnRealWindows, () => {
    const d = home();
    const fake = fakeIcacls({ readBody: aclLine(d, "tessa") });
    try {
      windowsRestrictToOwner(d, { directory: true });
      assert.equal(fake.args()[3], "tessa:(OI)(CI)(F)",
        "a directory's ACE must be inheritable by its future contents, or "
        + "files created inside it are back on the inherited ACL");
    } finally {
      fake.restore();
    }
  });

test("windowsOwnerPrincipal qualifies the account with its domain when there is one",
  () => {
    assert.equal(windowsOwnerPrincipal({ USERNAME: "tessa" }), "tessa");
    assert.equal(
      windowsOwnerPrincipal({ USERNAME: "tessa", USERDOMAIN: "CORP" }),
      "CORP\\tessa",
      "a bare username on a domain-joined box can resolve to a different "
      + "account than the one that owns the file");
    assert.throws(() => windowsOwnerPrincipal({ USERNAME: "  " }),
      CredentialPermissionError,
      "with no account to restrict TO, the only safe move is to refuse");
  });

test("THROW SITE: icacls missing from PATH refuses to write the credential",
  skipOnRealWindows, () => {
    const f = path.join(home(), "creds");
    fs.writeFileSync(f, "");
    const empty = fs.mkdtempSync(path.join(os.tmpdir(), "nhempty-"));
    const savedPath = process.env.PATH;
    const savedUser = process.env.USERNAME;
    process.env.PATH = empty;             // no icacls anywhere on it
    process.env.USERNAME = "tessa";
    try {
      assert.throws(() => windowsRestrictToOwner(f),
        (err) => err instanceof CredentialPermissionError
          && /not found on PATH/.test(err.message),
        "a missing icacls must FAIL CLOSED. Returning quietly writes a "
        + "credential every administrator on the box can read");
    } finally {
      process.env.PATH = savedPath;
      if (savedUser === undefined) delete process.env.USERNAME;
      else process.env.USERNAME = savedUser;
      fs.rmSync(empty, { recursive: true, force: true });
    }
  });

test("THROW SITE: a non-zero icacls exit is never treated as success",
  skipOnRealWindows, () => {
    const f = path.join(home(), "creds");
    fs.writeFileSync(f, "");
    const fake = fakeIcacls({ readBody: aclLine(f, "tessa"), grantRc: 5 });
    try {
      assert.throws(() => windowsRestrictToOwner(f),
        (err) => err instanceof CredentialPermissionError
          && /icacls exited 5/.test(err.message));
    } finally {
      fake.restore();
    }
  });

test("THROW SITE: an unreadable ACL is unverified, so it is refused",
  skipOnRealWindows, () => {
    const f = path.join(home(), "creds");
    fs.writeFileSync(f, "");
    const fake = fakeIcacls({ readBody: aclLine(f, "tessa"), readRc: 3 });
    try {
      assert.throws(() => windowsRestrictToOwner(f),
        (err) => err instanceof CredentialPermissionError
          && /cannot verify permissions/.test(err.message),
        "the grant may have succeeded, but unverified is not verified");
    } finally {
      fake.restore();
    }
  });

test("THROW SITE: an ACL listing NO grantees cannot confirm the restriction",
  skipOnRealWindows, () => {
    const f = path.join(home(), "creds");
    fs.writeFileSync(f, "");
    const fake = fakeIcacls({ readBody: "" });      // summary lines only
    try {
      assert.throws(() => windowsRestrictToOwner(f),
        (err) => err instanceof CredentialPermissionError
          && /listed no grantees/.test(err.message),
        "an empty listing is the shape a silently-failed grant takes");
    } finally {
      fake.restore();
    }
  });

test("SYSTEM + Administrators + owner is ACCEPTED — they are the platform TCB",
  skipOnRealWindows, () => {
    // What a correctly-secured file looks like when the OWNER is a local admin
    // (the common case, and the CI runner's): /inheritance:r cannot strip the
    // EXPLICIT Administrators/SYSTEM ACEs an admin-owned file carries. These are
    // POSIX root's analog — the 0600 POSIX branch leaves root with access too —
    // so accepting them realigns Windows with the contract this module mirrors.
    const f = path.join(home(), "creds");
    fs.writeFileSync(f, "");
    const fake = fakeIcacls({ readBody: [
      aclLine(f, "NT AUTHORITY\\SYSTEM"),
      "BUILTIN\\Administrators:(I)(F)",
      "tessa:(I)(F)",
    ].join("\n") });
    try {
      assert.doesNotThrow(() => windowsRestrictToOwner(f),
        "SYSTEM and the local Administrators group are the platform TCB; "
        + "excluding them is impossible on an admin-owned file and stricter "
        + "than the POSIX 0600 contract this mirrors");
    } finally {
      fake.restore();
    }
  });

test("THROW SITE: a surviving NON-TCB account is refused, and named",
  skipOnRealWindows, () => {
    // The real guarantee for a NON-admin user: any other account that can still
    // reach the credential is the fail-closed signal. TCB principals alongside
    // it are accepted and must NOT be blamed in the message.
    const f = path.join(home(), "creds");
    fs.writeFileSync(f, "");
    const fake = fakeIcacls({ readBody: [
      aclLine(f, "NT AUTHORITY\\SYSTEM"),
      "BUILTIN\\Administrators:(I)(F)",
      "tessa:(I)(F)",
      "BUILTIN\\Users:(RX)",          // a genuinely other principal
    ].join("\n") });
    try {
      assert.throws(() => windowsRestrictToOwner(f),
        (err) => {
          if (!(err instanceof CredentialPermissionError)) return false;
          // The owner legitimately appears in the "Fix the ACL with: icacls …
          // /grant:r <owner>" hint, so only the "readable by …" segment names
          // culprits. The TCB and the owner must not be blamed there.
          const readable = err.message.split("readable by")[1]?.split(". ")[0] ?? "";
          return /BUILTIN\\Users/.test(readable)
            && !/BUILTIN\\Administrators/.test(readable)
            && !/NT AUTHORITY\\SYSTEM/.test(readable)
            && !/tessa/.test(readable);
        },
        "a non-owner, non-TCB account on the credential is THE defect: the "
        + "token readable by another standard user while the module reports a "
        + "private file. The TCB and the owner must not be named as culprits");
    } finally {
      fake.restore();
    }
  });

test("a case-different account name is the SAME account, not an intruder",
  skipOnRealWindows, () => {
    // Windows account names are case-insensitive. Comparing them case-sensitively
    // fails closed on a CORRECTLY secured file, which is a different bug and
    // one that would be blamed on this guard.
    const f = path.join(home(), "creds");
    fs.writeFileSync(f, "");
    const fake = fakeIcacls({ readBody: aclLine(f, "TESSA") });
    try {
      assert.doesNotThrow(() => windowsRestrictToOwner(f));
    } finally {
      fake.restore();
    }
  });

// ---------------------------------------------------------------------------
// THE CONTAINMENT CHECK — declared last, and it OBSERVES rather than asserts
// that the flipping above was tidy.
// ---------------------------------------------------------------------------

test("no test above left the platform flipped", () => {
  assert.equal(process.platform, REAL_PLATFORM,
    "a test leaked its faked platform. Every later credential write in this "
    + "process would dispatch to icacls");
  assert.deepEqual(Object.getOwnPropertyDescriptor(process, "platform"),
    REAL_PLATFORM_DESC, "process.platform was left redefined");

  // Not just the flag — the BEHAVIOUR. Under a leaked win32 this chmod would
  // never happen and the assertion below would fail.
  const h = home();
  const real = fs.chmodSync;
  let chmodded = 0;
  fs.chmodSync = (p, mode) => { chmodded += 1; return real(p, mode); };
  try {
    writeToken("sk-ant-oat-restored", h);
  } finally {
    fs.chmodSync = real;
  }
  assert.ok(chmodded >= 1,
    "restrictToOwner did not take the POSIX path after this file's win32 "
    + "tests, so something is still dispatching to Windows");
  assert.equal(fs.statSync(path.join(h, ".no_human", ".env")).mode & 0o777, 0o600);
});
