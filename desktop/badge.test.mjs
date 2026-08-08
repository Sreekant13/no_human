import test from "node:test";
import assert from "node:assert/strict";
import { parseBadgeCount, overlayBadgeBitmap } from "./badge.mjs";

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

// The Windows overlay badge is drawn by hand (setOverlayIcon renders nothing
// for us), so these test the actual pixels — a wrong font bit or a BGRA/RGBA
// channel swap would otherwise only be visible on a live taskbar.

const px = (img, x, y) => img.data.subarray((y * img.width + x) * 4, (y * img.width + x) * 4 + 4);

test("overlay bitmap has the documented shape: 32×32 BGRA", () => {
  const img = overlayBadgeBitmap(3);
  assert.equal(img.width, 32);
  assert.equal(img.height, 32);
  assert.equal(img.data.length, 32 * 32 * 4);
});

test("the disc is red in BGRA channel order, transparent at the corners", () => {
  const img = overlayBadgeBitmap(1);
  // center row, off-center column (avoids glyph strokes): disc red #D93025
  const p = px(img, 5, 16);
  assert.deepEqual([...p], [0x25, 0x30, 0xD9, 0xFF]); // B,G,R,A — a swap fails here
  assert.equal(px(img, 0, 0)[3], 0, "corner must be transparent");
  assert.equal(px(img, 31, 31)[3], 0, "corner must be transparent");
});

test("each displayable count renders distinct white pixels", () => {
  const seen = new Set();
  for (let n = 1; n <= 9; n++) {
    const img = overlayBadgeBitmap(n);
    const white = [];
    for (let i = 0; i < img.data.length; i += 4) {
      if (img.data[i] === 0xFF && img.data[i + 1] === 0xFF && img.data[i + 2] === 0xFF) white.push(i);
    }
    assert.ok(white.length > 0, `count ${n} drew no glyph`);
    seen.add(white.join(","));
  }
  assert.equal(seen.size, 9, "two different counts rendered identically");
});

test("ten and beyond render the same 9+ face; the exact count travels in the description, not the pixels", () => {
  assert.deepEqual(overlayBadgeBitmap(10), overlayBadgeBitmap(147));
  // and 9+ must differ from 9 itself
  assert.notDeepEqual(overlayBadgeBitmap(10), overlayBadgeBitmap(9));
});
