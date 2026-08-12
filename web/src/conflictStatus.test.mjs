import test from "node:test";
import assert from "node:assert/strict";

import { conflictRoundLabel, showConflictBadge } from "./conflictStatus.js";

// SCRUM-42: the badge is driven by SCRUM-41's pr_conflict_rounds context field
// (surfaced on the summary payload), never by feedback-text matching.

test("label renders round/max from real server data; falsy rounds -> null", () => {
  assert.equal(
    conflictRoundLabel({ pr_conflict_rounds: 2 }),
    "resolving merge conflict — round 2",
  );
  // With the configured bound on the payload the denominator is REAL data.
  assert.equal(
    conflictRoundLabel({ pr_conflict_rounds: 4, max_pr_conflict_rounds: 5 }),
    "resolving merge conflict — round 4/5",
  );
  assert.equal(conflictRoundLabel({ pr_conflict_rounds: 0 }), null);
  assert.equal(conflictRoundLabel({}), null);
  assert.equal(conflictRoundLabel(undefined), null);
});

test("the watcher's post-MERGEABLE reset (rounds -> 0) clears the badge", () => {
  assert.ok(showConflictBadge({ status: "implementing", pr_conflict_rounds: 1 }));
  assert.equal(showConflictBadge({ status: "implementing", pr_conflict_rounds: 0 }), false);
});

test("round counter clamps at the cap", () => {
  assert.equal(
    conflictRoundLabel({ pr_conflict_rounds: 3, max_pr_conflict_rounds: 3 }),
    "resolving merge conflict — round 3/3",
  );
  // The escalating-wake case: wake.py stores rounds = max+1 before escalating.
  const label = conflictRoundLabel({ pr_conflict_rounds: 4, max_pr_conflict_rounds: 3 });
  assert.equal(label, "resolving merge conflict — round 3/3");
  assert.ok(!label.includes("4/3"), label);
});

test("below the cap is unchanged", () => {
  assert.equal(
    conflictRoundLabel({ pr_conflict_rounds: 4, max_pr_conflict_rounds: 5 }),
    "resolving merge conflict — round 4/5",
  );
});

test("no cap surfaces no denominator", () => {
  const label = conflictRoundLabel({ pr_conflict_rounds: 4 });
  assert.equal(label, "resolving merge conflict — round 4");
  assert.ok(!label.includes("/"), label);
});

test("badge shows only in pipeline statuses — never sticky in Review PR or terminal lanes", () => {
  for (const status of ["pending", "context", "planning", "implementing", "reviewing", "testing"]) {
    assert.ok(showConflictBadge({ status, pr_conflict_rounds: 1 }), status);
  }
  // The rebase push returns the task to awaiting_approval with rounds still >0
  // until the watcher confirms MERGEABLE — the card must read normally there.
  for (const status of ["awaiting_approval", "done", "failed", "escalated", "awaiting_input"]) {
    assert.equal(showConflictBadge({ status, pr_conflict_rounds: 2 }), false, status);
  }
});
