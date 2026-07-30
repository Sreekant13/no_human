import { test } from "node:test";
import assert from "node:assert/strict";
import { httpPrUrl } from "./prUrl.js";

// ONE guard for every PR-link surface (board card, TaskTable row, drawer chip):
// only a real http(s) URL may become an href. Anything else — demo-DB
// local-pr://, a javascript: payload, a scheme that merely STARTS with the
// letters "http" — degrades to a text-only badge (null here).

test("https:// passes through unchanged", () => {
  assert.equal(httpPrUrl("https://example.com/repo/pull/13"), "https://example.com/repo/pull/13");
});

test("http:// passes through unchanged", () => {
  assert.equal(httpPrUrl("http://example.com/repo/pull/13"), "http://example.com/repo/pull/13");
});

test("UPPERCASE HTTPS:// still links (schemes are case-insensitive)", () => {
  assert.equal(httpPrUrl("HTTPS://Example.com/PR/1"), "HTTPS://Example.com/PR/1");
});

test("javascript: never yields an href", () => {
  assert.equal(httpPrUrl("javascript:alert(1)"), null);
});

test("http:javascript:alert(1) — http scheme WITHOUT // — never yields an href", () => {
  // startsWith(\"http\") let this through; the anchored ^https?:// must not.
  assert.equal(httpPrUrl("http:javascript:alert(1)"), null);
});

test("httpjavascript: (scheme merely starting with the letters http) is rejected", () => {
  assert.equal(httpPrUrl("httpjavascript:alert(1)"), null);
});

test("demo-DB local-pr:// degrades to text-only (null)", () => {
  assert.equal(httpPrUrl("local-pr://tasks/14"), null);
});

test("null / undefined / empty are null, not a crash", () => {
  assert.equal(httpPrUrl(null), null);
  assert.equal(httpPrUrl(undefined), null);
  assert.equal(httpPrUrl(""), null);
});
