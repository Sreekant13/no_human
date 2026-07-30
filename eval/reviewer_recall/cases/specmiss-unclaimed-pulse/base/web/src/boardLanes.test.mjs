import test from "node:test";
import assert from "node:assert/strict";
import { LANES, routeTask, isNeedsYou, isWaiting, isRealFailure, BOARD_LANES, OUTCOME_LANES } from "./boardLanes.js";

test("awaiting_approval routes to its OWN 'Review PR' lane, not a catch-all", () => {
  assert.equal(routeTask({ status: "awaiting_approval" }), "review");
});

test("clarification/decision states route to 'Needs Answer', separate from PRs", () => {
  assert.equal(routeTask({ status: "awaiting_input" }), "answer");
  assert.equal(routeTask({ status: "escalated" }), "answer");
});

test("a PR-approval task and a clarification task land in DIFFERENT lanes", () => {
  // The whole point of the split: these must not share a column.
  assert.notEqual(
    routeTask({ status: "awaiting_approval" }),
    routeTask({ status: "awaiting_input" }),
  );
});

test("blocked splits on wake_condition: auto-resolving → Working, else → Needs Answer", () => {
  // No separate Waiting column: a self-resolving blocked task sits in Working,
  // marked parked on the card (isWaiting), not in a mostly-empty column.
  assert.equal(routeTask({ status: "blocked", blocker_wake_condition: "ci_green_on:main" }), "working");
  assert.equal(routeTask({ status: "blocked" }), "answer");
});

test("working / done / failed route as before; paused_quota joins Working", () => {
  assert.equal(routeTask({ status: "implementing" }), "working");
  assert.equal(routeTask({ status: "done" }), "done");
  assert.equal(routeTask({ status: "failed" }), "failed");
  assert.equal(routeTask({ status: "paused_quota" }), "working");
});

test("there is no separate Waiting lane anymore", () => {
  assert.equal(LANES.find((l) => l.key === "waiting"), undefined);
  assert.equal(LANES.length, 5);
});

test("isWaiting flags parked-but-self-resolving tasks (the old Waiting column, on the card)", () => {
  assert.equal(isWaiting({ status: "paused_quota" }), true);
  assert.equal(isWaiting({ status: "blocked", blocker_wake_condition: "ci_green_on:main" }), true);
  assert.equal(isWaiting({ status: "blocked" }), false);         // needs a human → not waiting
  assert.equal(isWaiting({ status: "implementing" }), false);    // actively working
  assert.equal(isWaiting({ status: "done" }), false);
});

test("an unknown status falls back to Working, never lost", () => {
  assert.equal(routeTask({ status: "some_new_state" }), "working");
});

test("Review PR and Needs Answer are both loud, human-facing lanes", () => {
  const review = LANES.find((l) => l.key === "review");
  const answer = LANES.find((l) => l.key === "answer");
  assert.ok(review.loud && review.needsYou);
  assert.ok(answer.loud && answer.needsYou);
  assert.equal(review.statuses.length, 1);            // only awaiting_approval
  assert.ok(answer.statuses.includes("escalated"));
});

test("Review PR sits immediately before Done and is review-coloured", () => {
  const keys = LANES.map((l) => l.key);
  assert.equal(keys.indexOf("review") + 1, keys.indexOf("done"));  // beside Done
  const review = LANES.find((l) => l.key === "review");
  const working = LANES.find((l) => l.key === "working");
  assert.equal(review.accent, "var(--c-review)");     // its own colour…
  assert.notEqual(review.accent, working.accent);      // …not Working's blue
});

test("Needs Answer (act to unblock) is a different colour from Failed (dead)", () => {
  const answer = LANES.find((l) => l.key === "answer");
  const failed = LANES.find((l) => l.key === "failed");
  assert.notEqual(answer.accent, failed.accent);  // both were identical red before
});

test("isNeedsYou matches the board's lanes exactly (the header-count fix)", () => {
  assert.equal(isNeedsYou({ status: "awaiting_approval" }), true);   // Review PR
  assert.equal(isNeedsYou({ status: "escalated" }), true);          // Needs Answer
  assert.equal(isNeedsYou({ status: "awaiting_input" }), true);     // Needs Answer
  // the case the status-only count missed: blocked WITHOUT a wake condition
  // sits in Needs Answer, so it DOES need you (header said 6, lanes showed 7).
  assert.equal(isNeedsYou({ status: "blocked" }), true);
  // …but blocked WITH a wake condition auto-resolves → Waiting → not "needs you".
  assert.equal(isNeedsYou({ status: "blocked", blocker_wake_condition: "ci_green_on:main" }), false);
  assert.equal(isNeedsYou({ status: "implementing" }), false);
  assert.equal(isNeedsYou({ status: "done" }), false);
  assert.equal(isNeedsYou({ status: "failed" }), false);
});

// A cancelled task ends in FAILED status but is not a capability failure. The board's
// overview strip counted it as one, so "11 failed" was really 1 failure + 10 cancels —
// a permanent red alarm, 91% self-inflicted, competing with the real gates. Stats got
// this right (Stats.jsx) while the board did not; this is the shared definition.
test("isRealFailure excludes operator-cancelled tasks", () => {
  assert.equal(isRealFailure({ status: "failed", cancelled: false }), true);
  assert.equal(isRealFailure({ status: "failed" }), true);
  assert.equal(isRealFailure({ status: "failed", cancelled: true }), false);
  assert.equal(isRealFailure({ status: "done" }), false);
  assert.equal(isRealFailure({ status: "awaiting_input" }), false);
  assert.equal(isRealFailure(null), false);
  assert.equal(isRealFailure(undefined), false);
});

// 5D (operator): the board shows only the three GATE lanes. Done and Failed are outcomes, not
// gates — they leave the board and live behind two buttons that open a table.
test("the board renders exactly three lanes: needs-answer, working, review", () => {
  assert.deepEqual(BOARD_LANES.map((l) => l.key), ["answer", "working", "review"]);
});

test("the outcome lanes are still LANES — dropping them from the board must not drop them from ROUTING", () => {
  // The trap: filter LANES itself and routeTask can no longer place a done/failed task, so it
  // falls through to "working" and finished work reappears as in-flight.
  assert.equal(routeTask({ status: "done" }), "done");
  assert.equal(routeTask({ status: "failed" }), "failed");
  assert.deepEqual(OUTCOME_LANES.map((l) => l.key), ["failed", "done"]);
  // ...and the gate lanes still route as before.
  assert.equal(routeTask({ status: "awaiting_input" }), "answer");
  assert.equal(routeTask({ status: "implementing" }), "working");
  assert.equal(routeTask({ status: "awaiting_approval" }), "review");
});

test("each outcome lane carries the outline colour its button uses", () => {
  const failed = OUTCOME_LANES.find((l) => l.key === "failed");
  const done = OUTCOME_LANES.find((l) => l.key === "done");
  assert.match(failed.accent, /--c-escalated/);   // red
  assert.match(done.accent, /--c-done/);          // green
});

test("B2 #19: an approved PR stops shouting in 'need you' but keeps its lane", () => {
  const approved = { id: "a", status: "awaiting_approval", approved_at: "2026-07-15T00:00:00Z" };
  const unreviewed = { id: "b", status: "awaiting_approval" };

  assert.equal(isNeedsYou(approved), false, "approved must leave the need-you count");
  assert.equal(isNeedsYou(unreviewed), true, "un-reviewed still needs the human");

  // Routing is UNTOUCHED — LANES is also the routing table (the known trap):
  // the approved task stays visible in the Review lane.
  assert.equal(routeTask(approved), routeTask(unreviewed));
});
