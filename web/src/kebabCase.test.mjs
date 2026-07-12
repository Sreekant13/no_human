import test from "node:test";
import assert from "node:assert/strict";
import { kebabCase } from "./kebabCase.js";

test("camelCase splits on the case boundary", () => {
  assert.equal(kebabCase("camelCase"), "camel-case");
});

test("PascalCase lowercases the leading word", () => {
  assert.equal(kebabCase("PascalCase"), "pascal-case");
});

test("snake_case underscores become hyphens", () => {
  assert.equal(kebabCase("snake_case"), "snake-case");
});

test("spaces become hyphens", () => {
  assert.equal(kebabCase("space separated"), "space-separated");
});

test("mixed delimiters normalize together", () => {
  assert.equal(kebabCase("mixedCASE_with spaces"), "mixed-case-with-spaces");
});

test("empty string stays empty", () => {
  assert.equal(kebabCase(""), "");
});

test("single char is unchanged", () => {
  assert.equal(kebabCase("a"), "a");
});

test("consecutive delimiters collapse", () => {
  assert.equal(kebabCase("foo__bar  baz"), "foo-bar-baz");
});
