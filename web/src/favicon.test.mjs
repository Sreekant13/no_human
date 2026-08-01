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

// The two tests above pass for ANY svg — a blank one included. They checked
// the dot and the mime type, which is how the tab icon stayed the pre-rebrand
// chevron badge while both tests were green. Pin the artwork itself.
test("the base icon actually carries the brand mark", () => {
  const svg = decodeURIComponent(faviconHref(false));
  assert.ok(svg.includes("data:image/png;base64,"),
    "the mark must be embedded, not referenced — an href to a path renders blank in a data-URI favicon");
  const b64 = svg.split("base64,")[1].split("'")[0];
  assert.ok(b64.length > 4000, `embedded mark looks truncated: ${b64.length} chars`);
  assert.equal(Buffer.from(b64, "base64").subarray(0, 8).toString("hex"),
    "89504e470d0a1a0a", "embedded bytes are not a PNG");
});
