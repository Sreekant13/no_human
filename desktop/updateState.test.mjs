// A deferral that does not survive a restart is not a deferral.
//
// These write and read REAL files in a temp dir rather than a mocked fs: the
// bug this guards against (state that round-trips in memory but never reaches
// disk) is invisible to a mock.
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { STATE_FILE, readUpdateState, writeUpdateState } from "./updateState.mjs";

function tmp() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "nh-update-state-"));
}

test("state round-trips through the real filesystem", () => {
  const dir = tmp();
  assert.equal(writeUpdateState(dir, { deferredVersion: "0.2.0", lastCheckAt: 7 }), true);
  const read = readUpdateState(dir);
  assert.equal(read.deferredVersion, "0.2.0");
  assert.equal(read.lastCheckAt, 7);
  assert.ok(fs.existsSync(path.join(dir, STATE_FILE)),
    "the state must exist on disk, not merely in the return value");
});

test("the directory is created when it does not exist yet", () => {
  // First launch: userData may not have been written to before.
  const dir = path.join(tmp(), "nested", "deeper");
  assert.equal(writeUpdateState(dir, { deferredVersion: "0.3.0" }), true);
  assert.equal(readUpdateState(dir).deferredVersion, "0.3.0");
});

test("a missing file reads as empty state rather than throwing", () => {
  assert.deepEqual(readUpdateState(tmp()), {});
  assert.deepEqual(readUpdateState("/nonexistent/path/at/all"), {});
});

test("a corrupt file reads as empty state rather than crashing the launch", () => {
  const dir = tmp();
  fs.writeFileSync(path.join(dir, STATE_FILE), "{not json at all");
  assert.deepEqual(readUpdateState(dir), {},
    "a hand-mangled preferences file must not prevent the app from starting");
});

test("valid JSON that is not an object is rejected", () => {
  // `null`, `[]` and `"x"` all parse. Spreading them into state either throws
  // or silently produces indexed keys.
  const dir = tmp();
  for (const junk of ["null", "[1,2]", '"a string"', "42"]) {
    fs.writeFileSync(path.join(dir, STATE_FILE), junk);
    assert.deepEqual(readUpdateState(dir), {}, `${junk} must read as no state`);
  }
});

test("an unwritable location reports failure instead of throwing", () => {
  // A read-only home must never take down app startup.
  const dir = tmp();
  fs.chmodSync(dir, 0o500);
  const target = path.join(dir, "sub");
  const ok = writeUpdateState(target, { deferredVersion: "0.2.0" });
  fs.chmodSync(dir, 0o700);
  assert.equal(ok, false, "failure must be reported as a value, not an exception");
});

test("writing replaces prior state rather than merging behind the caller's back", () => {
  // updatePolicy.deferVersion already returns the merged object; a second
  // merge here would resurrect keys the caller deliberately dropped.
  const dir = tmp();
  writeUpdateState(dir, { deferredVersion: "0.2.0", lastCheckAt: 1 });
  writeUpdateState(dir, { lastCheckAt: 2 });
  const read = readUpdateState(dir);
  assert.equal(read.lastCheckAt, 2);
  assert.equal(read.deferredVersion, undefined,
    "the store must persist exactly what it was given");
});
