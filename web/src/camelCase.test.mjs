import test from "node:test";
import assert from "node:assert/strict";
import { camelCase } from "./camelCase.js";

test("kebab-case hyphens become camelCase", () => {
  assert.equal(camelCase("foo-bar"), "fooBar");
});

test("kebab-case with more words", () => {
  assert.equal(camelCase("foo-bar-baz"), "fooBarBaz");
});

test("kebab-case single hyphen pair", () => {
  assert.equal(camelCase("two-words"), "twoWords");
});

test("snake_case underscores become camelCase", () => {
  assert.equal(camelCase("foo_bar"), "fooBar");
});

test("snake_case with more words", () => {
  assert.equal(camelCase("foo_bar_baz"), "fooBarBaz");
});

test("snake_case single underscore pair", () => {
  assert.equal(camelCase("two_words"), "twoWords");
});

test("space separated words become camelCase", () => {
  assert.equal(camelCase("foo bar"), "fooBar");
});

test("space separated with more words", () => {
  assert.equal(camelCase("foo bar baz"), "fooBarBaz");
});

test("space separated single space pair", () => {
  assert.equal(camelCase("two words"), "twoWords");
});

test("mixed delimiters normalize together", () => {
  assert.equal(camelCase("foo-bar_baz qux"), "fooBarBazQux");
});

test("single word is unchanged", () => {
  assert.equal(camelCase("foo"), "foo");
});

test("empty string stays empty", () => {
  assert.equal(camelCase(""), "");
});

test("single character is unchanged", () => {
  assert.equal(camelCase("a"), "a");
});

test("consecutive delimiters collapse", () => {
  assert.equal(camelCase("foo__bar  baz"), "fooBarBaz");
});

test("leading and trailing separators are stripped", () => {
  assert.equal(camelCase("-foo-bar-"), "fooBar");
});

test("already-uppercase input is lowercased on the first word", () => {
  assert.equal(camelCase("FOO-BAR"), "fooBar");
});
