import test from "node:test";
import assert from "node:assert/strict";
import { truncateMiddle } from "./truncateMiddle.js";

test("a short string is returned unchanged", () => {
  assert.equal(truncateMiddle("abc", 10), "abc");
});

test("an exact-length string is returned unchanged", () => {
  assert.equal(truncateMiddle("abcde", 5), "abcde");
});

test("a long string is truncated with an ellipsis in the middle", () => {
  const result = truncateMiddle("feature/very-long-branch-name", 11);
  assert.ok(result.includes("…"));
  assert.equal(result, "featu…-name");
});

test("the truncated result length is exactly max", () => {
  const result = truncateMiddle("feature/very-long-branch-name", 11);
  assert.ok(result.length <= 11);
  assert.equal(result.length, 11);
});

test("the head/tail budget splits as ceil/floor of max-1", () => {
  const max = 11;
  const result = truncateMiddle("feature/very-long-branch-name", max);
  const [head, tail] = result.split("…");
  assert.equal(head.length, Math.ceil((max - 1) / 2));
  assert.equal(tail.length, Math.floor((max - 1) / 2));
});

test("guards against max < 1 without throwing", () => {
  assert.equal(truncateMiddle("abc", 0), "");
});
