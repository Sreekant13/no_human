import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { escalationTs, partitionAnswerLane, STALE_ANSWER_MS, shouldResetStaleOpen } from "./answerLane.js";

const NOW = new Date("2026-07-25T12:00:00Z").getTime();

test("fresh is ordered newest-escalation-first", () => {
  const items = [
    { id: "a", created_at: "2026-07-25T09:00:00Z" },
    { id: "b", created_at: "2026-07-25T11:00:00Z" },
    { id: "c", created_at: "2026-07-25T10:00:00Z" },
  ];
  const { fresh } = partitionAnswerLane(items, NOW);
  assert.deepEqual(fresh.map((x) => x.id), ["b", "c", "a"]);
});

test("escalated_at overrides created_at for ordering", () => {
  const items = [
    // Newer created_at, but its escalated_at is older than the other's.
    { id: "recent-create", created_at: "2026-07-25T11:00:00Z", escalated_at: "2026-07-25T08:00:00Z" },
    { id: "recent-escalate", created_at: "2026-07-25T01:00:00Z", escalated_at: "2026-07-25T09:00:00Z" },
  ];
  const { fresh } = partitionAnswerLane(items, NOW);
  assert.deepEqual(fresh.map((x) => x.id), ["recent-escalate", "recent-create"]);
});

test("splits on the 24h threshold: recent stays fresh, >24h goes stale", () => {
  const items = [
    { id: "recent", created_at: "2026-07-25T11:00:00Z" },   // 1h old
    { id: "old", created_at: "2026-07-20T12:00:00Z" },       // days old
  ];
  const { fresh, stale } = partitionAnswerLane(items, NOW);
  assert.deepEqual(fresh.map((x) => x.id), ["recent"]);
  assert.deepEqual(stale.map((x) => x.id), ["old"]);
});

test("boundary: exactly at the threshold stays fresh (> not >=)", () => {
  const exactlyThreshold = new Date(NOW - STALE_ANSWER_MS).toISOString();
  const items = [{ id: "boundary", created_at: exactlyThreshold }];
  const { fresh, stale } = partitionAnswerLane(items, NOW);
  assert.deepEqual(fresh.map((x) => x.id), ["boundary"]);
  assert.equal(stale.length, 0);

  const oneMsOver = new Date(NOW - STALE_ANSWER_MS - 1).toISOString();
  const overItems = [{ id: "just-over", created_at: oneMsOver }];
  const over = partitionAnswerLane(overItems, NOW);
  assert.deepEqual(over.stale.map((x) => x.id), ["just-over"]);
  assert.equal(over.fresh.length, 0);
});

test("counts are truthful: fresh.length + stale.length === items.length, nothing dropped or mutated", () => {
  const items = [
    { id: "a", created_at: "2026-07-25T11:00:00Z" },
    { id: "b", created_at: "2026-07-01T00:00:00Z" },
    { id: "c" }, // no timestamp at all
    { id: "d", created_at: "2026-07-25T10:59:00Z" },
  ];
  const snapshot = JSON.parse(JSON.stringify(items));
  const { fresh, stale } = partitionAnswerLane(items, NOW);
  assert.equal(fresh.length + stale.length, items.length);
  assert.deepEqual(items, snapshot, "input array must not be mutated");
});

test("ties break deterministically by id", () => {
  const items = [
    { id: "c", created_at: "2026-07-25T10:00:00Z" },
    { id: "a", created_at: "2026-07-25T10:00:00Z" },
    { id: "b", created_at: "2026-07-25T10:00:00Z" },
  ];
  const { fresh } = partitionAnswerLane(items, NOW);
  assert.deepEqual(fresh.map((x) => x.id), ["a", "b", "c"]);
});

test("empty/undefined input returns empty lists, never throws", () => {
  assert.deepEqual(partitionAnswerLane([], NOW), { fresh: [], stale: [] });
  assert.deepEqual(partitionAnswerLane(undefined, NOW), { fresh: [], stale: [] });
  assert.deepEqual(partitionAnswerLane(null, NOW), { fresh: [], stale: [] });
});

test("missing timestamp goes to stale, sorts last, never throws", () => {
  const items = [
    { id: "has-ts", created_at: "2026-07-01T00:00:00Z" }, // old, but has a timestamp
    { id: "no-ts" },
  ];
  const { stale } = partitionAnswerLane(items, NOW);
  assert.deepEqual(stale.map((x) => x.id), ["has-ts", "no-ts"]);
});

test("escalationTs fallback chain: escalated_at > last_activity > updated_at > created_at", () => {
  assert.equal(
    escalationTs({ escalated_at: "E", last_activity: "L", updated_at: "U", created_at: "C" }),
    "E",
  );
  assert.equal(
    escalationTs({ last_activity: "L", updated_at: "U", created_at: "C" }),
    "L",
  );
  assert.equal(escalationTs({ updated_at: "U", created_at: "C" }), "U");
  assert.equal(escalationTs({ created_at: "C" }), "C");
  assert.equal(escalationTs({}), "");
  assert.equal(escalationTs(null), "");
});

test("shouldResetStaleOpen: true when open and stale group empty", () => {
  assert.equal(shouldResetStaleOpen(true, 0), true);
});

test("shouldResetStaleOpen: no reset while stale cards remain", () => {
  assert.equal(shouldResetStaleOpen(true, 3), false);
  assert.equal(shouldResetStaleOpen(true, 1), false);
});

test("shouldResetStaleOpen: no-op when already collapsed", () => {
  assert.equal(shouldResetStaleOpen(false, 0), false);
});

// Static source-shape assertion: no jsdom/React renderer is wired into this
// project's `node --test` harness (see sidebarNav.test.mjs), so we can't mount
// Board.jsx and observe staleOpen actually reset. Instead pin the wiring itself —
// the effect must call the tested predicate with the right args and reset the
// right state — so deleting/miswiring the useEffect fails this test even though
// the pure-predicate tests above stay green.
test("Board.jsx wires the stale-open reset effect to shouldResetStaleOpen(staleOpen, stale.length)", () => {
  const here = fileURLToPath(new URL(".", import.meta.url));
  const boardJsx = readFileSync(here + "Board.jsx", "utf8");
  assert.match(
    boardJsx,
    /useEffect\(\(\) => \{\s*if \(shouldResetStaleOpen\(staleOpen, stale\.length\)\) setStaleOpen\(false\);\s*\}, \[staleOpen, stale\.length\]\);/,
    "expected a useEffect that resets staleOpen via shouldResetStaleOpen(staleOpen, stale.length), deps [staleOpen, stale.length]",
  );
});
