import test from "node:test";
import assert from "node:assert/strict";
import {
  isAiConfigDone, markAiConfigDone, isPopupDismissed, markPopupDismissed,
} from "./aiConfigNudge.js";

function fakeStorage(initial = {}) {
  const m = { ...initial };
  return {
    getItem: (k) => (k in m ? m[k] : null),
    setItem: (k, v) => { m[k] = String(v); },
    _dump: () => m,
  };
}

test("a fresh install is NOT done — the nudge shows", () => {
  assert.equal(isAiConfigDone(fakeStorage()), false);
});

test("marking done makes it read back done", () => {
  const s = fakeStorage();
  markAiConfigDone(s);
  assert.equal(isAiConfigDone(s), true);
  assert.equal(s._dump()["nh-ai-config-done"], "1");
});

test("a getItem that throws fails open to NOT done, never crashes", () => {
  const throwing = { getItem: () => { throw new Error("blocked"); } };
  assert.equal(isAiConfigDone(throwing), false);
});

test("a setItem that throws is swallowed", () => {
  const throwing = { setItem: () => { throw new Error("blocked"); } };
  assert.doesNotThrow(() => markAiConfigDone(throwing));
});

test("a null storage (localStorage unavailable) is not-done and no-op", () => {
  assert.equal(isAiConfigDone(null), false);
  assert.doesNotThrow(() => markAiConfigDone(null));
});

// ── the popup's own bit (fix round, 2026-09-01): separate from the badge's,
// so opening Settings on any pane kills the popup without satisfying the
// badge's stricter "the Second-brain pane was actually seen" condition. ── //

test("a fresh install has NOT dismissed the popup — it shows", () => {
  assert.equal(isPopupDismissed(fakeStorage()), false);
});

test("marking the popup dismissed makes it read back dismissed, under its OWN key", () => {
  const s = fakeStorage();
  markPopupDismissed(s);
  assert.equal(isPopupDismissed(s), true);
  assert.equal(s._dump()["nh-ai-config-popup-dismissed"], "1");
  // Distinct key from the badge's — dismissing the popup must not, by
  // itself, touch the badge's own storage slot.
  assert.equal(s._dump()["nh-ai-config-done"], undefined);
});

test("popup: a getItem that throws fails open to NOT dismissed, never crashes", () => {
  const throwing = { getItem: () => { throw new Error("blocked"); } };
  assert.equal(isPopupDismissed(throwing), false);
});

test("popup: a setItem that throws is swallowed", () => {
  const throwing = { setItem: () => { throw new Error("blocked"); } };
  assert.doesNotThrow(() => markPopupDismissed(throwing));
});

test("popup: a null storage (localStorage unavailable) is not-dismissed and no-op", () => {
  assert.equal(isPopupDismissed(null), false);
  assert.doesNotThrow(() => markPopupDismissed(null));
});

// ── integration (review I1): opening Settings without ever visiting the
// Second-brain pane must kill the popup PERMANENTLY, while the badge must
// keep showing until that pane is actually seen. This is the exact defect
// the fix round reported: the D2.1 first cut used ONE flag for both, so a
// user who opened Settings via the row body or a Finish-setup deep link and
// never visited Second-brain got the popup back on every subsequent visit. */
test("integration: opening Settings (any pane) permanently dismisses the popup but never satisfies the badge", () => {
  const s = fakeStorage();
  assert.equal(isPopupDismissed(s), false);
  assert.equal(isAiConfigDone(s), false);

  // Simulates App.jsx's openSettings(): fires regardless of which pane the
  // overlay lands on.
  markPopupDismissed(s);
  assert.equal(isPopupDismissed(s), true, "the popup must never show again after any Settings open");
  assert.equal(isAiConfigDone(s), false, "the badge must still show — the Second-brain pane was never visited");

  // A second, third, ... Settings open (still never visiting Second-brain)
  // must not resurrect the popup or force the badge condition either way.
  markPopupDismissed(s);
  assert.equal(isPopupDismissed(s), true);
  assert.equal(isAiConfigDone(s), false);

  // Only once the Second-brain pane itself renders does the badge clear.
  markAiConfigDone(s);
  assert.equal(isAiConfigDone(s), true);
  assert.equal(isPopupDismissed(s), true, "already-dismissed popup stays dismissed");
});
