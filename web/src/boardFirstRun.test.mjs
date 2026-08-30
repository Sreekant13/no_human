import test from "node:test";
import assert from "node:assert/strict";
import { isFirstRun } from "./boardFirstRun.js";

test("first run only when nothing has ever been filed", () => {
  assert.equal(isFirstRun([], 0), true);
  assert.equal(isFirstRun([{ id: "a" }], 0), false);
  assert.equal(isFirstRun([], 3), false);
});
