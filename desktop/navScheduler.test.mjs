// The navigation scheduler. Both of its failure modes shipped once, so both
// are pinned here.
import assert from "node:assert/strict";
import test from "node:test";

import { createNavScheduler } from "./navScheduler.mjs";

const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));

test("runs one at a time, in order — never concurrently", async () => {
  const events = [];
  let live = 0;
  const s = createNavScheduler(async (name) => {
    live += 1;
    assert.equal(live, 1, "two navigations ran at once");
    events.push(`start:${name}`);
    await tick(20);
    events.push(`end:${name}`);
    live -= 1;
  });
  await Promise.all([s.schedule("a"), s.schedule("b"), s.schedule("c")]);
  assert.deepEqual(events,
    ["start:a", "end:a", "start:b", "end:b", "start:c", "end:c"]);
});

test("each caller gets its OWN run — results are not coalesced", async () => {
  let runs = 0;
  const s = createNavScheduler(async () => { runs += 1; await tick(10); });
  await Promise.all([s.schedule(), s.schedule(), s.schedule()]);
  assert.equal(runs, 3,
    "joining an in-flight nav made a token save report success off someone else's run");
});

test("supersede() stands down a QUEUED navigation, not just the running one", async () => {
  const painted = [];
  const s = createNavScheduler(async (name, isCurrent) => {
    await tick(20);
    if (!isCurrent()) return;             // stood down
    painted.push(name);
  });
  const a = s.schedule("a");
  const b = s.schedule("b");              // queued behind a
  s.supersede();                          // e.g. the user opened the setup screen
  await Promise.all([a, b]);
  assert.deepEqual(painted, [],
    "a queued nav re-took ownership and painted over the credential screen");
});

test("a navigation scheduled AFTER supersede still runs", async () => {
  const painted = [];
  const s = createNavScheduler(async (name, isCurrent) => {
    if (isCurrent()) painted.push(name);
  });
  s.supersede();
  await s.schedule("fresh");
  assert.deepEqual(painted, ["fresh"]);
});

test("a rejection does not wedge the queue", async () => {
  const done = [];
  const s = createNavScheduler(async (name) => {
    if (name === "boom") throw new Error("nope");
    done.push(name);
  });
  await assert.rejects(s.schedule("boom"));
  await s.schedule("after");
  assert.deepEqual(done, ["after"]);
});

test("a hung navigation times out and releases the queue", async () => {
  const done = [];
  const s = createNavScheduler(async (name) => {
    if (name === "hang") return new Promise(() => {});   // never settles
    done.push(name);
  }, 40);
  await assert.rejects(s.schedule("hang"), /timed out/);
  await s.schedule("after");
  assert.deepEqual(done, ["after"], "everything after a hang was blocked forever");
});

test("a timed-out navigation is stood down, and pending frees up", async () => {
  let stillCurrentAfterTimeout = null;
  const s = createNavScheduler(async (name, isCurrent) => {
    if (name === "hang") {
      await tick(120);                      // outlives the 40ms timeout
      stillCurrentAfterTimeout = isCurrent();
      return;
    }
  }, 40);
  await assert.rejects(s.schedule("hang"), /timed out/);
  await tick(150);
  assert.equal(stillCurrentAfterTimeout, false,
    "an abandoned nav must not still be able to paint or spawn");
  assert.equal(s.pending, 0, "Retry would be dead forever otherwise");
});

test("pending reports queue depth and returns to 0", async () => {
  const s = createNavScheduler(async () => { await tick(15); });
  assert.equal(s.pending, 0);
  const a = s.schedule(); const b = s.schedule();
  assert.equal(s.pending, 2, "used to coalesce Retry spam; now it must be observable");
  await Promise.all([a, b]);
  assert.equal(s.pending, 0);
});

test("scheduleIfIdle: declines while a nav is running, runs when idle", async () => {
  let runs = 0;
  const s = createNavScheduler(async () => { runs += 1; await tick(30); });
  const first = s.scheduleIfIdle();
  assert.notEqual(first, null, "the first click must run");
  assert.equal(s.scheduleIfIdle(), null, "a second click while busy is ignored");
  assert.equal(s.scheduleIfIdle(), null);
  await first;
  assert.equal(runs, 1, "repeated Retry each booted another server");
  assert.notEqual(s.scheduleIfIdle(), null, "and it works again once idle");
});

test("scheduleIfIdle: a timed-out nav does not block Retry forever", async () => {
  const s = createNavScheduler(async (name) => {
    if (name === "hang") return new Promise(() => {});
  }, 40);
  await assert.rejects(s.scheduleIfIdle("hang"), /timed out/);
  assert.equal(s.pending, 0);
  assert.notEqual(s.scheduleIfIdle("after"), null, "Retry would be permanently dead");
});
