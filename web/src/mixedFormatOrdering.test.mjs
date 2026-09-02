// Repro for the mixed-format lexical-sort bug (independent-review advisory
// A1 on the 2026-09-01 UTC-skew fix, same incident family): the DB carries
// both naive-space 'YYYY-MM-DD HH:MM:SS' and iso-offset '...+00:00' strings
// in the same `*_at` column (src/no_human/core/db.py:3556-3558). Sorting
// those as raw strings is wrong because ' ' < 'T' lexically, so a
// naive-space row sorts before EVERY iso-offset row regardless of its real
// age. This file imports only the pre-existing board/lane/answer-lane
// modules (never the new parseTimestamp helper) so it reproduces red at the
// merge-base, before the fix lands.
//
// Fixture direction, verified empirically (not the surface reading of the
// ticket's prose): for any pair of same-calendar-day rows, raw string
// comparison diverges at the 11th character — ' ' (0x20) vs 'T' (0x54) —
// which comes BEFORE either string's clock digits. That means a naive-space
// row compares as lexically "less than" an iso-offset row on the same date
// NO MATTER what the clock digits say, so the only fixture that actually
// exercises the defect is one where the naive-space row is the genuinely
// NEWER row (later clock time) and the iso-offset row is the genuinely
// OLDER one — that's the case where lexical order and true chronological
// order disagree. A naive-older/iso-newer pairing (as a literal reading of
// the ticket's prose example might suggest) happens to have lexical order
// and chronological order agree by coincidence and does NOT reproduce red;
// confirmed by running it against the pre-fix code (all three functions
// below already passed with that pairing — see PR description for the
// transcript). This file uses the pairing that actually goes red pre-fix.
import test from "node:test";
import assert from "node:assert/strict";
import { groupFailedByTitle } from "./boardGroups.js";
import { topByRecency } from "./laneView.js";
import { partitionAnswerLane } from "./answerLane.js";

const NAIVE_NEWER = "2026-01-01 11:00:00"; // naive-space, genuinely later clock time
const ISO_OLDER = "2026-01-01T10:00:00+00:00"; // iso-offset, genuinely earlier clock time

test("laneView.topByRecency: a genuinely newer naive-space row sorts ahead of an older iso-offset row", () => {
  const items = [
    { id: "iso-old", updated_at: ISO_OLDER },
    { id: "naive-new", updated_at: NAIVE_NEWER },
  ];
  const { visible } = topByRecency(items, 2);
  assert.deepEqual(
    visible.map((x) => x.id),
    ["naive-new", "iso-old"],
    "the genuinely newer naive-space row must lead, not the older iso-offset one",
  );
});

test("boardGroups.groupFailedByTitle: the newer naive-space row heads the group over an older iso-offset one", () => {
  const rows = groupFailedByTitle([
    { id: "iso-old", title: "Same title", status: "failed", created_at: ISO_OLDER },
    { id: "naive-new", title: "Same title", status: "failed", created_at: NAIVE_NEWER },
  ]);
  assert.equal(rows.length, 1);
  assert.equal(
    rows[0].task.id,
    "naive-new",
    "the newer naive-space row must head the group, not the older iso-offset one",
  );
  assert.deepEqual(rows[0].olderIds, ["iso-old"]);
});

test("answerLane.partitionAnswerLane: fresh-ordering leads with the newer naive-space row", () => {
  const nowMs = Date.parse("2026-01-01T11:30:00Z"); // 30 min after the newer row
  const items = [
    { id: "iso-old", created_at: ISO_OLDER },
    { id: "naive-new", created_at: NAIVE_NEWER },
  ];
  const { fresh } = partitionAnswerLane(items, nowMs);
  assert.deepEqual(
    fresh.map((x) => x.id),
    ["naive-new", "iso-old"],
    "the newer naive-space row must sort first within fresh",
  );
});
