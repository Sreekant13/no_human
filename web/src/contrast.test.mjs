import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

// Guards ONE bug class: a text role whose resolved colour token does not reach
// WCAG AA against the surface it is actually painted on.
//
// It was found by measuring the LIVE board (Playwright, computed styles,
// ancestor opacity composited) rather than by reading the stylesheet: in the
// dark theme thirteen 10-12px roles resolved through a single token,
// `--text-dim: #5C6375`, and landed at 2.80:1 on a card and 3.14:1 on the
// canvas. Small text needs 4.5:1. `.card-id` was worse still (2.02:1) because
// it ALSO carried `opacity: 0.7` on top of the already-dim token.
//
// This is deliberately an arithmetic assertion over resolved token values, not
// a screenshot: a screenshot tells you something changed, a ratio tells you
// whether a human can read it. Every surface below is a hex MEASURED off the
// running board (see the provenance note on each), so the numbers are not
// idealised — `#1b1e28` really is what a card composites to, and the decision
// panel really does sit on a red-tinted `#31232b`, not on `--bg-card`.

const SRC = dirname(fileURLToPath(import.meta.url));
// Strip comments first: a commented-out declaration must not count, and the
// file's header comments quote token values verbatim.
const css = readFileSync(join(SRC, "styles.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

const AA_SMALL = 4.5;

// ── colour maths (WCAG 2.1 relative luminance) ──────────────────────────────
const channels = (hex) => {
  const h = hex.replace("#", "").trim();
  assert.match(h, /^[0-9a-fA-F]{6}$/, `not a 6-digit hex colour: ${hex}`);
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
};

const luminance = (hex) => {
  const [r, g, b] = channels(hex).map((v) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};

const contrast = (fg, bg) => {
  const [hi, lo] = [luminance(fg), luminance(bg)].sort((a, b) => b - a);
  return (hi + 0.05) / (lo + 0.05);
};

// ── token resolution ────────────────────────────────────────────────────────
// Read the two theme blocks out of the stylesheet so the assertions below track
// the real values. Matching `[data-theme="light"]` as a RULE, not by indexOf on
// the bare string, is deliberate — themeVars.test.mjs pays for that lesson: the
// bare string hits the file's header comment first.
const blockAfter = (re) => {
  const m = css.match(re);
  assert.ok(m, `expected to find the rule ${re}`);
  return m[1];
};

const declarations = (block) => {
  const out = new Map();
  for (const m of block.matchAll(/(--[a-zA-Z0-9-]+)\s*:\s*([^;]+);/g)) out.set(m[1], m[2].trim());
  return out;
};

const THEME = {
  dark: declarations(blockAfter(/:root\s*\{([^}]*)\}/)),
  light: declarations(blockAfter(/\[data-theme="light"\]\s*\{([^}]*)\}/)),
};

// Resolve a token to a hex, following `var(--other)` aliases (`--fg-dim` is an
// alias for `--text-dim`). Light falls back to dark, which is exactly how the
// cascade behaves and is the hole themeVars.test.mjs guards separately.
function resolve(token, theme, seen = new Set()) {
  assert.ok(!seen.has(token), `circular token alias at ${token}`);
  seen.add(token);
  const raw = THEME[theme].get(token) ?? THEME.dark.get(token);
  assert.ok(raw, `${token} is not defined in either theme block`);
  const alias = raw.match(/^var\(\s*(--[a-zA-Z0-9-]+)\s*\)$/);
  return alias ? resolve(alias[1], theme, seen) : raw;
}

// Every colour token a selector can resolve to: the base rule, ancestor-state
// overrides (`.task-card.stale .card-age` repaints the age amber) and its own
// pseudo-class states (`:hover`). A role is only legible if EVERY state it can
// be in is legible — a hover that repaints the label back under AA is the same
// bug. The leading boundary matters: `.card-id` is a substring of
// `.memory-card-id`.
function colorTokensOf(selector) {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const pseudo = "(?::[a-z-]+(?:\\([^)]*\\))?)*";
  const tokens = new Set();
  for (const m of css.matchAll(new RegExp(`(?:^|[\\s,{}>])${esc}${pseudo}\\s*\\{([^}]*)\\}`, "g"))) {
    const decl = m[1].match(/(?:^|[\s;])color\s*:\s*([^;]+)/);
    if (!decl) continue;
    const token = decl[1].match(/var\(\s*(--[a-zA-Z0-9-]+)/);
    assert.ok(token, `${selector} sets \`color: ${decl[1].trim()}\` — a literal cannot be theme-checked`);
    tokens.add(token[1]);
  }
  assert.ok(tokens.size > 0, `no \`color\` rule found for ${selector} — has it been renamed?`);
  return [...tokens];
}

// ── surfaces, measured on the running board ─────────────────────────────────
// Keys are what a human calls the place; values are the composited background
// hex Playwright read off `getComputedStyle` there, per theme.
const SURFACE = {
  // The board canvas — `--bg` / `--bg-card` resolved straight from the tokens.
  canvas: { dark: () => resolve("--bg", "dark"), light: () => resolve("--bg", "light") },
  card: { dark: () => resolve("--bg-card", "dark"), light: () => resolve("--bg-card", "light") },
  // A task card as it actually composites on the lane (the lane tint shows
  // through), measured: dark #1b1e28, light #fefefe. Very slightly WORSE than
  // the bare --bg-card in dark, so it is asserted in its own right.
  boardCard: { dark: () => "#1b1e28", light: () => "#fefefe" },
  // The slide-over is --glass-bg (72% alpha) over the scrimmed page, measured
  // as #171a23 in dark and #ffffff in light.
  drawer: { dark: () => "#171a23", light: () => "#ffffff" },
};

// Which surfaces each role is painted on. Derived from a live sweep of the
// board, the drawer (every tab) and the Stats page — not from reading the JSX.
const ROLES = [
  [".card-age", ["boardCard", "card"]],
  [".card-id", ["boardCard", "card"]],
  [".card-source", ["boardCard", "card"]],
  [".card-attempts", ["boardCard", "card"]],
  [".card-substatus", ["boardCard", "card"]],
  [".stats-th", ["card"]],
  [".stats-card-label", ["card", "canvas"]],
  [".stats-card-sub", ["card", "canvas"]],
  [".stats-section-sub", ["canvas"]],
  [".ns-sub", ["canvas"]],
  [".nh-ledger-title", ["canvas"]],
  [".nh-status-label", ["canvas"]],
  [".lane-count", ["canvas"]],
  // .lane-more-text / .lane-more-arrow inherit their colour from the .lane-more
  // button, which repaints its own surface as a color-mix of --base and
  // --surface-1. That mix always sits BETWEEN the two, so --bg-card (the
  // lighter end in dark, the whiter end in light) is the conservative bound.
  [".lane-more", ["card"]],
  [".so-id", ["drawer"]],
  [".so-chip-sub", ["drawer"]],
  [".settings-overlay-group-header", ["drawer"]],
];

test("every dim text role clears WCAG AA on the surfaces it is painted on", () => {
  const failures = [];
  for (const [selector, surfaces] of ROLES) {
    for (const token of colorTokensOf(selector)) {
      for (const theme of ["dark", "light"]) {
        const fg = resolve(token, theme);
        for (const surface of surfaces) {
          const bg = SURFACE[surface][theme]();
          const ratio = contrast(fg, bg);
          if (ratio < AA_SMALL) {
            failures.push(
              `${selector} (${token} = ${fg}) on ${surface} ${bg} in ${theme}: `
              + `${ratio.toFixed(2)}:1 < ${AA_SMALL}:1`);
          }
        }
      }
    }
  }
  assert.deepEqual(failures, [], `WCAG AA failures for 10-12px text:\n  ${failures.join("\n  ")}`);
});

test("the decision panel's own dim step clears AA on its red-tinted surface", () => {
  // .decision-panel does NOT sit on --bg-card: `background: var(--red-dim)`
  // composites to #31232b in dark and #ffeceb in light (measured in the live
  // drawer). The global dim step is 4.15:1 / 4.46:1 there, so the panel
  // redefines --text-dim locally; this asserts the override actually works.
  const panel = blockAfter(/\.decision-panel\s*\{([^}]*)\}/);
  const override = panel.match(/--text-dim\s*:\s*var\(\s*(--[a-zA-Z0-9-]+)\s*\)/);
  assert.ok(override, ".decision-panel must scope --text-dim to a token that clears AA on --red-dim");

  const bg = { dark: "#31232b", light: "#ffeceb" };
  for (const theme of ["dark", "light"]) {
    const fg = resolve(override[1], theme);
    const ratio = contrast(fg, bg[theme]);
    assert.ok(ratio >= AA_SMALL,
      `.decision-panel dim (${override[1]} = ${fg}) on ${bg[theme]} in ${theme}: `
      + `${ratio.toFixed(2)}:1 < ${AA_SMALL}:1`);
  }

  // The destructive third choice is danger-tinted rather than dim, so its own
  // label has to clear AA on that same tinted surface — including the hover
  // state, where a red wash background would have pulled it back under.
  for (const token of colorTokensOf(".decision-btn-quiet")) {
    for (const theme of ["dark", "light"]) {
      const fg = resolve(token, theme);
      const ratio = contrast(fg, bg[theme]);
      assert.ok(ratio >= AA_SMALL,
        `"Stop this task" (${token} = ${fg}) on ${bg[theme]} in ${theme}: `
        + `${ratio.toFixed(2)}:1 < ${AA_SMALL}:1`);
    }
  }
});

test("--text-dim is never dimmed a second time with opacity", () => {
  // `.card-id` set `color: var(--text-dim)` AND `opacity: 0.7` (twice, which is
  // how it survived review), compositing to 2.02:1 in dark and 2.83:1 in light.
  // Raising the token cannot fix a role that then multiplies it back down, so
  // the two must never appear in the same rule.
  //
  // The one exemption is a 36px decorative glyph: AA's 4.5:1 floor applies to
  // small text, and an empty-state icon carries no information a label doesn't.
  const EXEMPT = [".stats-empty-icon"];
  const offenders = [];
  for (const m of css.matchAll(/([^{}]+)\{([^}]*)\}/g)) {
    const [, selector, body] = m;
    if (!/color\s*:\s*var\(\s*--(?:text|fg)-dim/.test(body)) continue;
    const opacity = body.match(/(?:^|[\s;])opacity\s*:\s*([\d.]+)/);
    if (!opacity || Number(opacity[1]) >= 1) continue;
    if (EXEMPT.some((e) => selector.includes(e))) continue;
    offenders.push(`${selector.trim()} → opacity ${opacity[1]}`);
  }
  assert.deepEqual(offenders, [],
    `these rules dim --text-dim twice, pushing them under AA:\n  ${offenders.join("\n  ")}`);
});

test("dim still reads as dim: it stays well below the body text step", () => {
  // The point of raising the token was legibility, not flattening the ramp. If
  // dim ever climbs to within 2x of --text-body against the same surface, the
  // hierarchy the board is built on has been erased and this is the wrong fix.
  for (const theme of ["dark", "light"]) {
    const surface = resolve("--bg-card", theme);
    const dim = contrast(resolve("--text-dim", theme), surface);
    const body = contrast(resolve("--text-body", theme), surface);
    assert.ok(body / dim >= 2,
      `${theme}: --text-dim is ${dim.toFixed(2)}:1 and --text-body ${body.toFixed(2)}:1 `
      + `on --bg-card — only ${(body / dim).toFixed(2)}x apart, the ramp has collapsed`);
  }
});
