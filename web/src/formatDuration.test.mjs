import test from "node:test";
import assert from "node:assert/strict";
import { formatDuration } from "./formatDuration.js";

test("seconds branch (<60) formats as Xs", () => {
  assert.equal(formatDuration(0), "0s");
  assert.equal(formatDuration(30), "30s");
  assert.equal(formatDuration(59), "59s");
});

test("60 boundary switches to minutes, not seconds", () => {
  assert.equal(formatDuration(59), "59s");
  assert.equal(formatDuration(60), "1m");
  assert.equal(formatDuration(90), "1m");
});

test("minutes upper edge floors just under the hour", () => {
  assert.equal(formatDuration(3599), "59m");
});

test("3600 boundary switches to hours, not minutes", () => {
  assert.equal(formatDuration(3599), "59m");
  assert.equal(formatDuration(3600), "1h");
  assert.equal(formatDuration(7199), "1h");
  assert.equal(formatDuration(7200), "2h");
});
