import test from "node:test";
import assert from "node:assert/strict";
import { parseBadgeCount } from "./badge.mjs";

test("parses the needs-you count from the web app's title format", () => {
  assert.equal(parseBadgeCount("(3) no_human"), 3);
  assert.equal(parseBadgeCount("(12) no_human"), 12);
});

test("the clean app title means zero", () => {
  assert.equal(parseBadgeCount("no_human"), 0);
});

test("foreign titles mean NO INFORMATION, never zero", () => {
  // The error page ("no_human — server not reachable") must not wipe a badge
  // that is still true; same for arbitrary parenthesised task names.
  assert.equal(parseBadgeCount("no_human — server not reachable"), null);
  assert.equal(parseBadgeCount("(WIP) fix retry"), null);
  assert.equal(parseBadgeCount("(3) something else"), null);
  assert.equal(parseBadgeCount(""), null);
  assert.equal(parseBadgeCount(undefined), null);
});
