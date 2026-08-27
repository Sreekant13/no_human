import { test } from "node:test";
import assert from "node:assert/strict";
import { splitRecent, relativeMtime, debounce } from "./repoRecency.js";

test("splitRecent takes the newest ≤limit within 30 days, rest keeps order", () => {
  const now = 1_700_000_000;
  const repos = [{name:"a", mtime: now-3600}, {name:"b", mtime: now-40*86400}, {name:"c", mtime: null}];
  const { recent, rest } = splitRecent(repos, now, { limit: 6 });
  assert.deepEqual(recent.map(r=>r.name), ["a"]); assert.deepEqual(rest.map(r=>r.name), ["b","c"]);
});
test("relativeMtime", () => { assert.equal(relativeMtime(1_700_000_000-7200, 1_700_000_000), "2h ago"); assert.equal(relativeMtime(null, 1), "—"); });

// limit bites: the 7th recent repo falls into rest even though it is in-window.
test("splitRecent caps at limit and the overflow keeps newest-first order in rest", () => {
  const now = 2_000_000_000;
  const repos = Array.from({ length: 8 }, (_, i) => ({ name: `r${i}`, mtime: now - i * 3600 }));
  const { recent, rest } = splitRecent(repos, now, { limit: 6 });
  assert.deepEqual(recent.map(r=>r.name), ["r0","r1","r2","r3","r4","r5"]);
  assert.deepEqual(rest.map(r=>r.name), ["r6","r7"]);
});

// debounce: only the last call inside the window runs, once. Fake BOTH timer
// primitives — the cancellation is the whole point, so clearTimeout has to
// really drop the superseded timer for this to prove anything.
test("debounce collapses a burst into a single trailing call", () => {
  const calls = [];
  const timers = new Map();
  let seq = 0;
  const realST = globalThis.setTimeout, realCT = globalThis.clearTimeout;
  globalThis.setTimeout = (fn) => { const id = ++seq; timers.set(id, fn); return id; };
  globalThis.clearTimeout = (id) => { timers.delete(id); };
  try {
    const d = debounce((x) => calls.push(x), 400);
    d("a"); d("b"); d("c");
    [...timers.values()].forEach((fn) => fn());   // fire whatever survived
  } finally {
    globalThis.setTimeout = realST; globalThis.clearTimeout = realCT;
  }
  assert.deepEqual(calls, ["c"], "only the final call in the burst runs");
});
