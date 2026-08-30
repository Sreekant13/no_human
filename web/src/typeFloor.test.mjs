import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

// Guards ONE bug class: functional interface text set below the 12px craft floor.
//
// The board's chips, meta lines, badges and readouts had drifted to 10px/11px one
// rule at a time — each defensible alone, and collectively a surface the user has
// to squint at. impeccable's Operate-mode type floor (detector rules "functional
// text < 11px" and "body < 12px") puts the bottom of the scale at 12px for
// anything a user has to READ.
//
// There is deliberately NO allow-list. An exception here is a selector the next
// author copies; the moment one is genuinely needed it belongs here WITH a comment
// naming why that text is decorative rather than read — not as a silent regex hole.
//
// The Tailwind screens (composer, Settings panes, Backlog) get the same floor from
// the second test below — their type comes from the utility scale, not from here.
//
// KNOWN BLIND SPOTS — neither test can see:
//   · inline `style={{ fontSize: … }}` in .jsx (Onboarding.jsx still carries
//     several 0.7–0.85rem literals, i.e. 9.8–11.9px; the drawer's one was raised
//     by hand in the same pass);
//   · `em` sizes, which resolve against a parent this file cannot know
//     (`.md .md-inline-code` is 0.9em of body text, ~12.6px — deliberate).
// Widening to .jsx means owning Onboarding too; do that in the change that fixes
// it, not by leaving a red test behind.

const SRC = dirname(fileURLToPath(import.meta.url));
// Strip comments first: a commented-out `font-size: 11px`, and the sizes quoted in
// this stylesheet's own header prose, must not count as live declarations. Each
// comment is replaced by its OWN newlines so the reported line numbers still point
// at the real styles.css — a stripped copy renumbers every rule below the header.
const css = readFileSync(join(SRC, "styles.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  (c) => "\n".repeat((c.match(/\n/g) || []).length),
);

const FLOOR_PX = 12;
// `rem` resolves against the root font-size, which this stylesheet itself sets on
// `html` — read it rather than assuming the browser default 16px, because it is 14px
// here and assuming 16 would score 0.75rem as 12px when it actually paints 10.5px.
const ROOT_PX = Number(
  css.match(/html,\s*body,\s*#root\s*\{[^}]*?font-size:\s*([\d.]+)px/)?.[1],
);

test("sanity: the root font-size is read from styles.css, not assumed", () => {
  assert.ok(Number.isFinite(ROOT_PX) && ROOT_PX > 0, "could not read the html font-size");
  assert.equal(ROOT_PX, 14);
});

test("no functional text below 12px", () => {
  // Line numbers, not byte offsets — the failure has to be actionable.
  const hits = [];
  css.split("\n").forEach((line, i) => {
    for (const m of line.matchAll(/font-size:\s*([\d.]+)(px|rem)\b/g)) {
      const px = m[2] === "rem" ? Number(m[1]) * ROOT_PX : Number(m[1]);
      if (px < FLOOR_PX) hits.push(`${i + 1}: ${line.trim()}  → ${px}px`);
    }
  });
  assert.deepEqual(
    hits,
    [],
    `found ${hits.length} sub-${FLOOR_PX}px font-size declarations in styles.css:\n${hits.join("\n")}`,
  );
});

// ── The Tailwind half of the same floor ─────────────────────────────────────
// The composer, the Settings panes and Backlog are styled with utilities, so a
// `font-size:` grep over styles.css is blind to them. Tailwind's default scale is
// in `rem` and assumes a 16px root; this app's root is 14px, which silently made
// `text-xs` 10.5px in the shipped bundle (measured, 2026-08-29). The guard is on
// the CONFIG rather than on class usage: a scale step that is legal at every call
// site is the fix, and it is the config that decides.
const config = readFileSync(join(SRC, "..", "tailwind.config.js"), "utf8");
const jsx = readdirSync(SRC)
  .filter((f) => f.endsWith(".jsx"))
  .map((f) => readFileSync(join(SRC, f), "utf8"))
  .join("\n");

test("every Tailwind type step this app uses lands at or above the floor", () => {
  // Tailwind 3's stock scale, in rem — the values that apply to any step the
  // config does NOT override.
  const STOCK_REM = { xs: 0.75, sm: 0.875, base: 1, lg: 1.125, xl: 1.25 };
  const overrides = Object.fromEntries(
    [...config.matchAll(/\b(xs|sm|base|lg|xl):\s*\[?\s*"([\d.]+)(px|rem)"/g)]
      .map((m) => [m[1], m[3] === "rem" ? Number(m[2]) * ROOT_PX : Number(m[2])]),
  );
  const used = new Set([...jsx.matchAll(/\btext-(xs|sm|base|lg|xl)\b/g)].map((m) => m[1]));
  assert.ok(used.size >= 2, `expected the Tailwind type utilities in the .jsx, found ${[...used]}`);

  const tooSmall = [...used]
    .map((step) => [step, overrides[step] ?? STOCK_REM[step] * ROOT_PX])
    .filter(([, px]) => px < FLOOR_PX)
    .map(([step, px]) => `text-${step} = ${px}px`);
  assert.deepEqual(
    tooSmall,
    [],
    `Tailwind steps below the ${FLOOR_PX}px floor at a ${ROOT_PX}px root: ${tooSmall.join(", ")}. ` +
      "Redefine the step in tailwind.config.js (theme.extend.fontSize) rather than " +
      "patching call sites.",
  );
});

test("no arbitrary Tailwind type value slips under the floor", () => {
  // `text-[11px]` bypasses the scale entirely. None exists today; this is the
  // guard that keeps the config fix above from being routed around.
  const bad = [...jsx.matchAll(/\btext-\[([\d.]+)(px|rem)\]/g)]
    .map((m) => [m[0], m[2] === "rem" ? Number(m[1]) * ROOT_PX : Number(m[1])])
    .filter(([, px]) => px < FLOOR_PX)
    .map(([cls, px]) => `${cls} = ${px}px`);
  assert.deepEqual(bad, [], `arbitrary Tailwind type under the floor: ${bad.join(", ")}`);
});
