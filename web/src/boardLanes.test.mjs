import test from "node:test";
import assert from "node:assert/strict";
import { LANES, routeTask } from "./boardLanes.js";

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

test("blocked splits on wake_condition: auto → Waiting, else → Needs Answer", () => {
  assert.equal(routeTask({ status: "blocked", blocker_wake_condition: "ci_green_on:main" }), "waiting");
  assert.equal(routeTask({ status: "blocked" }), "answer");
});

test("working / done / failed route as before", () => {
  assert.equal(routeTask({ status: "implementing" }), "working");
  assert.equal(routeTask({ status: "done" }), "done");
  assert.equal(routeTask({ status: "failed" }), "failed");
  assert.equal(routeTask({ status: "paused_quota" }), "waiting");
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
