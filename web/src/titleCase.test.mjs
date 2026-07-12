import test from "node:test";
import assert from "node:assert/strict";
import { titleCase } from "./titleCase.js";

test("basic two-word capitalization", () => {
  assert.equal(titleCase("hello world"), "Hello World");
});

test("mixed case normalizes to Title Case", () => {
  assert.equal(titleCase("hELLO wORLD"), "Hello World");
});

test("single word capitalizes", () => {
  assert.equal(titleCase("hello"), "Hello");
});

test("empty string stays empty", () => {
  assert.equal(titleCase(""), "");
});

test("collapses and trims surrounding/multiple spaces", () => {
  assert.equal(titleCase("  hello  world  "), "Hello World");
});

test("multiple internal spaces collapse", () => {
  assert.equal(titleCase("a   b"), "A B");
});
