// Guards the regression that Escape/secondary must never quit the app when a
// board is live behind the credential screen.
import assert from "node:assert/strict";
import test from "node:test";

import { dismissTarget, labels, parseCanReturn, restartFailedMessage, saveProgress } from "./setupUi.mjs";

test("parseCanReturn: only the explicit flag counts", () => {
  assert.equal(parseCanReturn("?canReturn=1"), true);
  assert.equal(parseCanReturn("?origin=x&canReturn=1"), true);
  assert.equal(parseCanReturn("?canReturn=0"), false);
  assert.equal(parseCanReturn("?canReturn=true"), false, "only \"1\"");
  assert.equal(parseCanReturn(""), false);
  assert.equal(parseCanReturn(undefined), false, "first run has no query");
});

test("dismissTarget: NEVER quit when a board is behind the screen", () => {
  assert.equal(dismissTarget(true), "dismiss",
    "quitting here would kill the app and stop a spawned server");
  assert.equal(dismissTarget(false), "quit",
    "genuine first run has nothing to dismiss to");
});

test("labels: the buttons describe what will actually happen", () => {
  const over = labels(true);
  assert.equal(over.secondary, "Back to board");
  assert.match(over.primary, /restart/, "saving restarts a running server");
  assert.match(over.hint, /interrupted/, "the interruption must be stated");

  const first = labels(false);
  assert.equal(first.secondary, "Quit");
  assert.equal(first.primary, "Save and start");
  assert.match(first.hint, /API key/);
});

test("saveProgress: never claims to stop an old server on first run", () => {
  // labels(false) already says "Save and START" — there is nothing to stop.
  for (const ms of [0, 5000, 20000, 40000]) {
    assert.doesNotMatch(saveProgress(ms, false), /old server/i,
      `first-run copy at ${ms}ms must not mention an old server`);
  }
  // Re-entry over a live board DOES stop one, and should say so.
  assert.match(saveProgress(5000, true), /old server/i);
});

test("saveProgress: the message advances so a 43s save cannot look like a hang", () => {
  const at = (ms) => saveProgress(ms, true);
  assert.equal(at(0), at(2999), "no churn in the first seconds");
  // Each stage must actually differ from the previous one, or the copy is inert.
  const stages = [at(0), at(5000), at(20000), at(40000)];
  assert.equal(new Set(stages).size, 4,
    `every stage must say something new, got ${JSON.stringify(stages)}`);
  assert.ok(stages.every((m) => typeof m === "string" && m.length > 0));
});

test("restartFailedMessage: names the action that actually helps, in both cases", () => {
  const ours = restartFailedMessage("http://127.0.0.1:8420", true);
  assert.match(ours, /quit no_human/i,
    "our own server: the only fix is to quit the app");
  assert.doesNotMatch(ours, /nh start|restart that server/i,
    "a friend on a packaged DMG has no terminal and never ran `nh start`");

  const theirs = restartFailedMessage("http://127.0.0.1:8420", false);
  assert.match(theirs, /another server/i);
  assert.doesNotMatch(theirs, /quit no_human/i,
    "quitting the app cannot free a port another process holds");
});
