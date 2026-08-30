import test from "node:test";
import assert from "node:assert/strict";
import { isAiConfigDone, markAiConfigDone } from "./aiConfigNudge.js";

function fakeStorage(initial = {}) {
  const m = { ...initial };
  return {
    getItem: (k) => (k in m ? m[k] : null),
    setItem: (k, v) => { m[k] = String(v); },
    _dump: () => m,
  };
}

test("a fresh install is NOT done — the nudge shows", () => {
  assert.equal(isAiConfigDone(fakeStorage()), false);
});

test("marking done makes it read back done", () => {
  const s = fakeStorage();
  markAiConfigDone(s);
  assert.equal(isAiConfigDone(s), true);
  assert.equal(s._dump()["nh-ai-config-done"], "1");
});

test("a getItem that throws fails open to NOT done, never crashes", () => {
  const throwing = { getItem: () => { throw new Error("blocked"); } };
  assert.equal(isAiConfigDone(throwing), false);
});

test("a setItem that throws is swallowed", () => {
  const throwing = { setItem: () => { throw new Error("blocked"); } };
  assert.doesNotThrow(() => markAiConfigDone(throwing));
});

test("a null storage (localStorage unavailable) is not-done and no-op", () => {
  assert.equal(isAiConfigDone(null), false);
  assert.doesNotThrow(() => markAiConfigDone(null));
});
