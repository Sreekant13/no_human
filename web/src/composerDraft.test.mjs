import test from "node:test";
import assert from "node:assert/strict";

import {
  DRAFT_KEY,
  DRAFT_FIELDS,
  saveDraft,
  saveDraftNow,
  loadDraft,
  clearDraft,
  mergeWithSeed,
  flushDraft,
  cancelPendingDraft,
} from "./composerDraft.js";

// Part 1 of the composer draft-recovery feature (a friend lost ~2500 typed
// characters to an accidental close). This is the pure persistence layer —
// no component, no React. See composerDraft.js for the defect narrative.

function memoryStorage() {
  const map = new Map();
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => {
      map.set(key, String(value));
    },
    removeItem: (key) => {
      map.delete(key);
    },
    _raw: map,
  };
}

function throwOnSetStorage() {
  const base = memoryStorage();
  return {
    ...base,
    setItem: () => {
      throw new Error("QuotaExceededError");
    },
  };
}

// A fake timer pair: setTimer records the callback instead of scheduling it,
// clearTimer counts how many times it was invoked. Firing is manual, so tests
// stay deterministic with no wall-clock dependency — the same idiom the rest
// of this repo's pure modules use for injected time.
function fakeTimers() {
  let nextHandle = 1;
  const armed = new Map();
  let clearCount = 0;
  return {
    setTimer: (fn) => {
      const handle = nextHandle++;
      armed.set(handle, fn);
      return handle;
    },
    clearTimer: (handle) => {
      clearCount += 1;
      armed.delete(handle);
    },
    fire: (handle) => {
      const fn = armed.get(handle);
      assert.ok(fn, `no timer armed for handle ${handle}`);
      armed.delete(handle);
      fn();
    },
    get clearCount() {
      return clearCount;
    },
    get armedCount() {
      return armed.size;
    },
  };
}

const SAMPLE = {
  prompt: "Fix the off-by-one in the paginator",
  title: "Fix pagination bug",
  repoPath: "/repos/widgets",
  kind: "bug",
  priority: "high",
  acceptance: "Page 1 shows exactly 20 items",
};

test("the four exported functions accept an injected storage", () => {
  // Sanity: this module must import cleanly with no real localStorage (the
  // Node default), and every exported entry point must take a storage param
  // rather than reaching for a global.
  const store = memoryStorage();
  assert.doesNotThrow(() => saveDraftNow(SAMPLE, store));
  assert.doesNotThrow(() => loadDraft(store));
  assert.doesNotThrow(() => clearDraft(store));
  assert.doesNotThrow(() => mergeWithSeed(SAMPLE, { prompt: "x" }));
  assert.equal(globalThis.localStorage, undefined, "this test must run with no real localStorage present");
});

test("a saved draft loads back field-for-field", () => {
  const store = memoryStorage();
  saveDraftNow(SAMPLE, store);
  assert.deepEqual(loadDraft(store), SAMPLE);
});

test("the debounced save round-trips through the fake timer", () => {
  const store = memoryStorage();
  const timers = fakeTimers();
  saveDraft(SAMPLE, { storage: store, setTimer: timers.setTimer, clearTimer: timers.clearTimer });
  assert.equal(store.getItem(DRAFT_KEY), null, "must not write before the timer fires");
  timers.fire(1);
  assert.deepEqual(loadDraft(store), SAMPLE);
});

test("clearDraft leaves loadDraft returning null", () => {
  const store = memoryStorage();
  saveDraftNow(SAMPLE, store);
  clearDraft(store);
  assert.equal(loadDraft(store), null);
});

test("clearDraft cancels a save already armed", () => {
  const store = memoryStorage();
  const timers = fakeTimers();
  const handle = saveDraft(SAMPLE, { storage: store, setTimer: timers.setTimer, clearTimer: timers.clearTimer });
  clearDraft(store);
  // The resurrection case: firing the callback that was captured BEFORE the
  // clear must not bring the draft back.
  assert.equal(timers.armedCount, 0, "clearDraft must cancel the outstanding timer");
  assert.throws(() => timers.fire(handle), /no timer armed/);
  assert.equal(loadDraft(store), null);
});

test("every field present in the seed beats the draft", () => {
  for (const field of DRAFT_FIELDS) {
    const seedValue = `${field}-from-seed`;
    const seed = { [field]: seedValue };
    const merged = mergeWithSeed(SAMPLE, seed);
    assert.equal(merged[field], seedValue, `${field} should come from the seed`);
    for (const other of DRAFT_FIELDS) {
      if (other === field) continue;
      assert.equal(merged[other], SAMPLE[other], `${other} should come from the draft`);
    }
  }
});

test("a field absent from the seed keeps the drafted value", () => {
  const merged = mergeWithSeed(SAMPLE, { prompt: "seeded prompt" });
  assert.equal(merged.title, SAMPLE.title);
  assert.equal(merged.repoPath, SAMPLE.repoPath);
  assert.equal(merged.kind, SAMPLE.kind);
  assert.equal(merged.priority, SAMPLE.priority);
  assert.equal(merged.acceptance, SAMPLE.acceptance);
});

test("an undefined seed field is absence, not an override", () => {
  const merged = mergeWithSeed(SAMPLE, { prompt: undefined, title: null, repoPath: "seeded" });
  assert.equal(merged.prompt, SAMPLE.prompt, "undefined seed field must not override the draft");
  assert.equal(merged.title, SAMPLE.title, "null seed field must not override the draft");
  assert.equal(merged.repoPath, "seeded", "a real seed value must still win");
});

test("corrupt stored JSON yields null instead of throwing", () => {
  const corruptValues = ["{not json", "", "null", "[1,2]", '"a string"', "42"];
  for (const raw of corruptValues) {
    const store = memoryStorage();
    store._raw.set(DRAFT_KEY, raw);
    let result;
    assert.doesNotThrow(() => {
      result = loadDraft(store);
    }, `loadDraft must not throw for ${JSON.stringify(raw)}`);
    assert.equal(result, null, `loadDraft must return null for ${JSON.stringify(raw)}`);
  }
});

test("three rapid saves produce exactly one write, carrying the last value", () => {
  const store = memoryStorage();
  const timers = fakeTimers();
  const opts = { storage: store, setTimer: timers.setTimer, clearTimer: timers.clearTimer };
  const first = saveDraft({ ...SAMPLE, prompt: "first" }, opts);
  const second = saveDraft({ ...SAMPLE, prompt: "second" }, opts);
  const third = saveDraft({ ...SAMPLE, prompt: "third" }, opts);
  assert.equal(store.getItem(DRAFT_KEY), null, "no write before the debounce fires");
  assert.equal(timers.clearCount, 2, "each re-arm must cancel the previous timer");
  timers.fire(third);
  const persisted = loadDraft(store);
  assert.equal(persisted.prompt, "third");
  assert.notEqual(first, second);
  assert.notEqual(second, third);
});

test("a credential-shaped field is not written to storage", () => {
  const store = memoryStorage();
  const dangerous = {
    prompt: "legit prompt",
    apiKey: "sk-super-secret",
    token: "tok-abc123",
    password: "hunter2",
    oauthToken: "oauth-xyz789",
  };
  saveDraftNow(dangerous, store);
  const raw = store.getItem(DRAFT_KEY);
  assert.ok(raw, "expected something to be persisted");
  for (const secret of ["sk-super-secret", "tok-abc123", "hunter2", "oauth-xyz789"]) {
    assert.ok(!raw.includes(secret), `stored draft must not contain ${secret}`);
  }
  const loaded = loadDraft(store);
  for (const key of ["apiKey", "token", "password", "oauthToken"]) {
    assert.equal(Object.prototype.hasOwnProperty.call(loaded, key), false, `${key} must not round-trip`);
  }
  assert.equal(loaded.prompt, "legit prompt");
});

test("a throwing setItem does not propagate", () => {
  const store = throwOnSetStorage();
  assert.doesNotThrow(() => {
    const persisted = saveDraftNow(SAMPLE, store);
    assert.equal(persisted, false);
  });
});

test("a null storage is a no-op, not a crash", () => {
  assert.doesNotThrow(() => {
    assert.equal(saveDraftNow(SAMPLE, null), false);
    assert.equal(loadDraft(null), null);
    assert.equal(clearDraft(null), false);
  });
  cancelPendingDraft();
});

test("flushDraft writes immediately and cancels the pending timer", () => {
  const store = memoryStorage();
  const timers = fakeTimers();
  const handle = saveDraft(SAMPLE, { storage: store, setTimer: timers.setTimer, clearTimer: timers.clearTimer });
  const persisted = flushDraft(SAMPLE, store, timers.clearTimer);
  assert.equal(persisted, true);
  assert.equal(timers.armedCount, 0);
  assert.deepEqual(loadDraft(store), SAMPLE);
  assert.throws(() => timers.fire(handle), /no timer armed/);
});
