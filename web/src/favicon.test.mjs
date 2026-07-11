import test from "node:test";
import assert from "node:assert/strict";
import { faviconHref } from "./favicon.js";

test("dot changes the href and only true has a red fill", () => {
  const withDot = faviconHref(true);
  const withoutDot = faviconHref(false);
  assert.notEqual(withDot, withoutDot);
  assert.ok(decodeURIComponent(withDot).includes("#FF3B30"));
  assert.ok(!decodeURIComponent(withoutDot).includes("#FF3B30"));
});

test("both are svg data URIs", () => {
  assert.ok(faviconHref(true).startsWith("data:image/svg+xml"));
  assert.ok(faviconHref(false).startsWith("data:image/svg+xml"));
});
