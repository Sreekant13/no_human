import test from "node:test";
import assert from "node:assert/strict";
import { formatBenchDate } from "./formatBenchDate.js";

test("formats ISO to short human date", () => {
  const iso = "2026-07-20T00:00:00+00:00";
  const expected = new Date(iso).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
  const result = formatBenchDate(iso);
  assert.equal(result, expected);
  assert.ok(!result.includes("T"));
  assert.notEqual(result, iso);
});

test("empty or missing returns empty string", () => {
  assert.equal(formatBenchDate(""), "");
  assert.equal(formatBenchDate(undefined), "");
  assert.equal(formatBenchDate(null), "");
});

test("unparseable falls back to raw input", () => {
  const result = formatBenchDate("not-a-date");
  assert.equal(result, "not-a-date");
  assert.notEqual(result, "Invalid Date");
});
