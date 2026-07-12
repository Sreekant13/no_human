import test from "node:test";
import assert from "node:assert/strict";
import { pluralize } from "./pluralize.js";

test("count of 1 returns the singular", () => {
  assert.equal(pluralize(1, "repo"), "repo");
});

test("count of 0 returns the plural", () => {
  assert.equal(pluralize(0, "repo", "repos"), "repos");
});

test("count of 2 returns the plural", () => {
  assert.equal(pluralize(2, "repo", "repos"), "repos");
});

test("defaults to singular + 's' when no plural is given", () => {
  assert.equal(pluralize(2, "repo"), "repos");
});

test("uses an explicit irregular plural", () => {
  assert.equal(pluralize(2, "entry", "entries"), "entries");
});
