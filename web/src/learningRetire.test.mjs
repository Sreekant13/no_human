import test from "node:test";
import assert from "node:assert/strict";
import { retireCandidates, retireLabel } from "./learningRetire.js";

test("null/missing last_used_at labels 'never used'", () => {
  assert.equal(retireLabel({ last_used_at: null }), "never used");
  assert.equal(retireLabel({}), "never used");
  assert.equal(retireLabel({ last_used_at: "garbage" }), "never used");
});

test("an old last_used_at labels with the day count", () => {
  const now = Date.parse("2026-08-12T00:00:00Z");
  const item = { last_used_at: "2026-05-01T00:00:00Z" };
  assert.equal(retireLabel(item, { now }), "unused for 103 days");
});

test("singular 'day' at exactly 1", () => {
  const now = Date.parse("2026-08-12T00:00:00Z");
  const item = { last_used_at: "2026-08-11T00:00:00Z" };
  assert.equal(retireLabel(item, { now }), "unused for 1 day");
});

test("fresh rows are excluded from candidates; stale and never-used survive", () => {
  const now = Date.parse("2026-08-12T00:00:00Z");
  const fresh = { id: "1", last_used_at: "2026-08-10T00:00:00Z" };  // 2 days
  const stale = { id: "2", last_used_at: "2026-05-01T00:00:00Z" };  // 103 days
  const never = { id: "3", last_used_at: null };
  const out = retireCandidates([fresh, stale, never], [], { days: 90, now });
  assert.deepEqual(out.map((c) => c.id), ["2", "3"]);
  assert.equal(out.find((c) => c.id === "2").label, "unused for 103 days");
  assert.equal(out.find((c) => c.id === "3").label, "never used");
});

test("empty/undefined input returns []", () => {
  assert.deepEqual(retireCandidates([], [], {}), []);
  assert.deepEqual(retireCandidates(undefined, undefined, {}), []);
});

test("a dismissed id never reappears", () => {
  const now = Date.parse("2026-08-12T00:00:00Z");
  const stale = { id: "2", last_used_at: "2026-05-01T00:00:00Z" };
  const out = retireCandidates([stale], ["2"], { days: 90, now });
  assert.deepEqual(out, []);
});
