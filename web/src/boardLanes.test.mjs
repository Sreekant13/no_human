import test from "node:test";
import assert from "node:assert/strict";
import { LANES, routeTask, isNeedsYou, isWaiting } from "./boardLanes.js";

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
