import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

// Review 2026-07-25 residue: the sticky lane chrome (.lane-more and
// .lane-stale-divider) must occlude the cards scrolling beneath it, be
// protected from flex squashing, and carry the app's focus ring. These are
// real <button>s pinned to the lane bottom — a transparent background lets
// card text bleed straight through the affordance.

const SRC = dirname(fileURLToPath(import.meta.url));
const raw = readFileSync(join(SRC, "styles.css"), "utf8");
const css = raw.replace(/\/\*[\s\S]*?\*\//g, "");

/** The declarations block of the FIRST rule whose selector list contains
 * `selector` exactly (comma-split, trimmed). */
function ruleBody(selector) {
  const re = /([^{}]+)\{([^{}]*)\}/g;
  for (const m of css.matchAll(re)) {
    const sels = m[1].split(",").map((s) => s.trim());
    if (sels.includes(selector)) return m[2];
  }
  return null;
}

// The lane's own surface, read from the .lane rule itself: if the lane's
// background mix ever drifts, the chrome must drift WITH it — hardcoding the
// value here would keep this green while occlusion-camouflage silently broke.
const laneBackground = (() => {
  for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const list = m[1].split(",").map((s) => s.trim());
    if (!list.includes(".lane")) continue;
    const bg = m[2].match(/background:\s*([^;]+);/);
    if (bg) return bg[1].trim();
  }
  return null;
})();

for (const sel of [".lane-stale-divider", ".lane-more"]) {
  test(`${sel}: sticky chrome paints the lane surface (never transparent)`, () => {
    assert.ok(laneBackground, "could not read .lane's own background");
    const body = ruleBody(sel);
    assert.ok(body, `no rule found for ${sel}`);
    assert.ok(body.includes("position: sticky"),
      `${sel} is expected to be sticky lane chrome`);
    assert.ok(!/background:\s*transparent/.test(body),
      `${sel} has a transparent background — cards scroll visibly through it`);
    assert.ok(body.includes(laneBackground),
      `${sel} must repaint the lane's own surface (${laneBackground})`);
  });
}

test(".task-card entrance animation never uses a forwards fill", () => {
  // A finished animation's forwards fill outranks every author rule forever:
  // `both` silently killed .answer-stale/.card-queued/.waiting-parked opacity
  // and the :hover lift (PR #42 staff-FE finding, proven with a live probe).
  let anim = null;
  for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const list = m[1].split(",").map((s) => s.trim());
    if (!list.includes(".task-card")) continue;
    const a = m[2].match(/animation:\s*([^;]+);/);
    if (a && a[1].includes("nh-card-arrive")) {
      anim = a[1];
      break;
    }
  }
  assert.ok(anim, "the nh-card-arrive animation declaration was not found");
  assert.ok(!/\b(both|forwards)\b/.test(anim),
    `nh-card-arrive must not fill forwards (got: ${anim}) — it pins opacity/transform above all author rules`);
});

test(".lane-stale-divider is in the lane flex-guard list", () => {
  // The lane flex-guard is the rule that pins `.lane-body > .task-card`.
  let sels = null;
  for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const list = m[1].split(",").map((s) => s.trim());
    if (list.includes(".lane-body > .task-card") && /flex:\s*0 0 auto/.test(m[2])) {
      sels = list;
      break;
    }
  }
  assert.ok(sels, "the lane flex-guard rule was not found");
  assert.ok(sels.includes(".lane-body > .lane-stale-divider"),
    `flex-guard covers ${sels.join(", ")} but not .lane-body > .lane-stale-divider`);
});

test(".lane-stale-divider carries the app focus ring", () => {
  const ring = css.match(/([^{}]+)\{[^{}]*outline:\s*2px solid var\(--accent-500\)/);
  assert.ok(ring, "the :focus-visible ring rule was not found");
  const sels = ring[1].split(",").map((s) => s.trim());
  assert.ok(sels.includes(".lane-stale-divider:focus-visible"),
    ".lane-stale-divider (a real <button>) is missing from the focus-ring list");
});

test(".task-card transitions opacity (answer-stale/card-queued hover must not snap)", () => {
  // The MAIN card rule (the one animating transform/box-shadow) — not the
  // reduced-motion `transition: none` override, which legitimately kills all.
  let transition = null;
  for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const list = m[1].split(",").map((s) => s.trim());
    if (!list.includes(".task-card")) continue;
    const t = m[2].match(/transition:\s*([^;]+);/);
    if (t && /transform/.test(t[1])) {
      transition = t[1];
      break;
    }
  }
  assert.ok(transition, "the main .task-card transition declaration was not found");
  assert.ok(/\bopacity\b/.test(transition),
    ".task-card transition list lacks opacity — .answer-stale/.card-queued hovers snap");
});
