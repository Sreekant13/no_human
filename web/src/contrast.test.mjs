import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

// Guards ONE bug class: small text whose resolved colour token does not reach
// WCAG AA against the surface it is actually painted on.
//
// The first version of this file hand-wrote a role → surface map, and the map
// was WRONG for every pill: `.card-source`, `.card-attempts`, `.lane-count`,
// `.decision-badge`, `.memory-count`, `.integration-kind`, `.grill-round-badge`
// and `.rich-tool-result` each carry their OWN `background: var(--bg-panel)` /
// `var(--surface-2)`, so their text sits on #22252F, not on the card or canvas
// the map claimed. Measured on the real fill they were still failing — and the
// test reported green. A guard that measures against the wrong background is
// worse than no guard, and this repo has already paid for that twice.
//
// So nothing is hand-mapped any more. The surface is DERIVED:
//   · a rule that declares its own `background` is checked against that fill,
//     resolved through the theme tokens (and composited over every ambient
//     surface when the fill is semi-transparent, e.g. --red-dim / --glass-bg);
//   · a rule that inherits its background is checked against EVERY ambient
//     surface token, worst case wins.
// A new pill added tomorrow is therefore checked against its own fill without
// anyone remembering to update a list.

const SRC = dirname(fileURLToPath(import.meta.url));
// Strip comments first: a commented-out declaration must not count, and the
// file's header comments quote token values and ratios verbatim.
const css = readFileSync(join(SRC, "styles.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

const AA_SMALL = 4.5;
// The tokens whose entire job is "small, secondary text". Both must clear AA.
const META_TOKENS = ["--text-dim", "--fg-dim", "--text-muted"];

// ── colour maths (WCAG 2.1 relative luminance) ──────────────────────────────
const parseColor = (value) => {
  const hex = value.match(/^#([0-9a-fA-F]{6})$/);
  if (hex) return [...[0, 2, 4].map((i) => parseInt(hex[1].slice(i, i + 2), 16)), 1];
  const rgba = value.match(/^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)\s*(?:[,/]\s*([\d.]+)\s*)?\)$/);
  if (rgba) return [+rgba[1], +rgba[2], +rgba[3], rgba[4] === undefined ? 1 : +rgba[4]];
  return null;
};

const luminance = ([r, g, b]) => {
  const [R, G, B] = [r, g, b].map((v) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * R + 0.7152 * G + 0.0722 * B;
};

const contrast = (fg, bg) => {
  const [hi, lo] = [luminance(fg), luminance(bg)].sort((a, b) => b - a);
  return (hi + 0.05) / (lo + 0.05);
};

const composite = (fg, bg) => fg.slice(0, 3).map((v, i) => v * fg[3] + bg[i] * (1 - fg[3]));

const show = ([r, g, b]) => "#" + [r, g, b].map((v) => Math.round(v).toString(16).padStart(2, "0")).join("");

test("sanity: the contrast maths agrees with published WCAG pairs", () => {
  // Without this the whole file could be confidently wrong in one direction.
  const near = (a, b) => Math.abs(a - b) < 0.02;
  assert.ok(near(contrast(parseColor("#000000"), parseColor("#ffffff")), 21), "black on white is 21:1");
  assert.ok(near(contrast(parseColor("#ffffff"), parseColor("#ffffff")), 1), "white on white is 1:1");
  assert.ok(near(contrast(parseColor("#767676"), parseColor("#ffffff")), 4.54), "#767676 on white is the AA boundary");
  assert.ok(near(contrast(parseColor("#595959"), parseColor("#ffffff")), 7.0), "#595959 on white is the AAA boundary");
});

// ── token resolution ────────────────────────────────────────────────────────
// Matching `[data-theme="light"]` as a RULE, not by indexOf on the bare string,
// is deliberate — themeVars.test.mjs pays for that lesson: the bare string hits
// the file's header comment first.
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

// Resolve a token to a colour, following `var(--other)` aliases (`--fg-dim` is
// an alias for `--text-dim`). Light falls back to dark, which is exactly how the
// cascade behaves and is the hole themeVars.test.mjs guards separately.
function resolveToken(token, theme, seen = new Set()) {
  assert.ok(!seen.has(token), `circular token alias at ${token}`);
  seen.add(token);
  const raw = THEME[theme].get(token) ?? THEME.dark.get(token);
  if (!raw) return null;
  const alias = raw.match(/^var\(\s*(--[a-zA-Z0-9-]+)\s*\)$/);
  return alias ? resolveToken(alias[1], theme, seen) : parseColor(raw);
}

// Every opaque surface token the app can paint text on. Derived from the theme
// block rather than typed out, so a new surface joins the worst-case set by
// itself. --surface-3 / --bg-hover are in here because dim text really does
// land on them: .so-close, .fx-log-close and .fx-role-row all set that fill on
// :hover, over descendants coloured with --text-dim.
const AMBIENT = Object.fromEntries(["dark", "light"].map((theme) => [
  theme,
  [...THEME[theme].keys()]
    .filter((t) => /^--(bg|base|surface-\d)/.test(t))
    .map((t) => ({ token: t, color: resolveToken(t, theme) }))
    .filter((s) => s.color && s.color[3] === 1),
]));

test("sanity: the ambient surface set is real and includes the pill fills", () => {
  for (const theme of ["dark", "light"]) {
    assert.ok(AMBIENT[theme].length >= 6, `${theme}: expected several ambient surfaces`);
    for (const required of ["--bg", "--bg-card", "--bg-panel", "--surface-2", "--surface-3"]) {
      assert.ok(AMBIENT[theme].some((s) => s.token === required),
        `${theme}: ${required} must be in the ambient surface set — it is a real text background`);
    }
  }
});

// ── rules ───────────────────────────────────────────────────────────────────
const RULES = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(([, selector, body]) => ({
  selector: selector.trim().replace(/\s+/g, " "),
  body,
}));

const decl = (body, prop) =>
  body.match(new RegExp(`(?:^|[\\s;])${prop}\\s*:\\s*([^;]+)`))?.[1].trim() ?? null;

// AA's 4.5:1 floor is for SMALL text; 24px+ (or 18.66px+ bold) is large text at
// 3:1. Only .stats-empty-icon, a 36px decorative glyph, is actually large here.
const isLarge = (body) => {
  const size = parseFloat(decl(body, "font-size") ?? "");
  if (!Number.isFinite(size)) return false;
  const weight = parseInt(decl(body, "font-weight") ?? "400", 10);
  return size >= 24 || (size >= 18.66 && weight >= 700);
};

/** The surfaces a rule's text can sit on, in one theme. */
function surfacesFor(body, theme) {
  const raw = decl(body, "background") ?? decl(body, "background-color");
  const ambient = AMBIENT[theme];
  if (!raw) return ambient.map((s) => ({ label: `inherited:${s.token}`, color: s.color }));

  // A gradient / color-mix / keyword tells us nothing usable, so fall back to
  // the worst ambient case rather than silently skipping the rule.
  const single = raw.match(/^var\(\s*(--[a-zA-Z0-9-]+)\s*\)$/);
  const own = single ? resolveToken(single[1], theme) : parseColor(raw);
  if (!own) return ambient.map((s) => ({ label: `unresolved(${raw}) over ${s.token}`, color: s.color }));

  if (own[3] === 1) return [{ label: `${single ? single[1] : raw} ${show(own)}`, color: own }];
  // Semi-transparent fill (--red-dim, --glass-bg): composite over each ambient.
  return ambient.map((s) => ({
    label: `${single ? single[1] : raw} over ${s.token} → ${show(composite(own, s.color))}`,
    color: composite(own, s.color),
  }));
}

test("every small-text meta role clears WCAG AA on the surface it is painted on", () => {
  const failures = [];
  let checked = 0;
  for (const { selector, body } of RULES) {
    const color = decl(body, "color");
    if (!color) continue;
    const token = color.match(/var\(\s*(--[a-zA-Z0-9-]+)/)?.[1];
    if (!token || !META_TOKENS.includes(token)) continue;
    // A 36px empty-state glyph is large text, and carries nothing a label does
    // not; it is the only rule this exempts, and it is exempted by MEASUREMENT
    // (its own font-size) rather than by name.
    if (isLarge(body)) continue;

    for (const theme of ["dark", "light"]) {
      const fg = resolveToken(token, theme);
      assert.ok(fg, `${token} does not resolve in ${theme}`);
      for (const surface of surfacesFor(body, theme)) {
        checked += 1;
        const ratio = contrast(fg, surface.color);
        if (ratio < AA_SMALL) {
          failures.push(`${selector} (${token} = ${show(fg)}) on ${surface.label} `
            + `in ${theme}: ${ratio.toFixed(2)}:1 < ${AA_SMALL}:1`);
        }
      }
    }
  }
  assert.ok(checked > 400, `expected the sweep to cover the stylesheet, only ${checked} checks ran`);
  assert.deepEqual([...new Set(failures)].sort(), [],
    `WCAG AA failures for small text:\n  ${[...new Set(failures)].sort().join("\n  ")}`);
});

test("the meta tokens are never dimmed a second time with opacity", () => {
  // `.card-id` set `color: var(--text-dim)` AND `opacity: 0.7` (twice, which is
  // how it survived review), compositing to 2.02:1 in dark and 2.83:1 in light.
  // Raising a token cannot fix a role that then multiplies it back down, so the
  // two must never appear in the same rule.
  const offenders = [];
  for (const { selector, body } of RULES) {
    const color = decl(body, "color");
    if (!color || !META_TOKENS.some((t) => color.includes(t))) continue;
    const opacity = parseFloat(decl(body, "opacity") ?? "1");
    if (opacity >= 1) continue;
    if (isLarge(body)) continue;
    offenders.push(`${selector} → opacity ${opacity}`);
  }
  assert.deepEqual(offenders, [],
    `these rules dim a meta token twice, pushing them under AA:\n  ${offenders.join("\n  ")}`);
});

test("dim and muted stay two visibly different steps, not one", () => {
  // This is the pair that actually collapsed. Raising --text-dim on its own
  // took dim-vs-muted from 1.93x to 1.16x, and on the --bg-panel pill fill that
  // put `.card-cancelled-badge` (muted) at the same weight as the neutral
  // `.card-source` / `.card-attempts` chips beside it in .card-meta — a state
  // badge reading as metadata. The earlier version of this test watched
  // dim-vs-BODY, which never moved and so never noticed.
  for (const theme of ["dark", "light"]) {
    for (const { token, color } of AMBIENT[theme]) {
      const dim = contrast(resolveToken("--text-dim", theme), color);
      const muted = contrast(resolveToken("--text-muted", theme), color);
      assert.ok(muted / dim >= 1.25,
        `${theme} on ${token}: dim ${dim.toFixed(2)}:1 vs muted ${muted.toFixed(2)}:1 — `
        + `only ${(muted / dim).toFixed(2)}x apart, the two meta steps have merged`);
    }
  }
});

test("dim still reads as dim: the ramp keeps its order and its distance", () => {
  for (const theme of ["dark", "light"]) {
    for (const { token, color } of AMBIENT[theme]) {
      const step = (t) => contrast(resolveToken(t, theme), color);
      assert.ok(step("--text-dim") < step("--text-muted"),
        `${theme} on ${token}: dim is no longer dimmer than muted`);
      assert.ok(step("--text-muted") < step("--text-body"),
        `${theme} on ${token}: muted is no longer quieter than body`);
      assert.ok(step("--text-body") / step("--text-dim") >= 1.7,
        `${theme} on ${token}: dim ${step("--text-dim").toFixed(2)}:1 vs body `
        + `${step("--text-body").toFixed(2)}:1 — the ramp has flattened`);
    }
  }
});

test('"Stop this task" stays legible on the panel it sits in', () => {
  // The destructive third choice is danger-tinted rather than dim, so its label
  // has to clear AA on .decision-panel's --red-dim fill — in both themes, and
  // in its :hover state, where a red wash background would pull it back under.
  //
  // The backdrop here is MEASURED, not inferred. The generic fallback above
  // composites a translucent fill over every ambient surface, which for
  // --red-dim includes the pill fills and produces #3b2d36 — a state the panel
  // cannot reach. Walking the live DOM (drawer open, both themes) gives its
  // real ancestor chain as exactly `.slideover` (--glass-bg) over --bg:
  //     dark  rgba(26,29,39,.72) over #0F1117  → --red-dim over that = #31232b
  //     light rgba(255,255,255,.72) over #F4F5F7 → --red-dim is opaque = #ffeceb
  // Asserting an unreachable composite would fabricate a failure, which is as
  // dishonest as missing a real one.
  const panel = RULES.find((r) => r.selector === ".decision-panel");
  assert.ok(panel, ".decision-panel must exist");
  assert.match(decl(panel.body, "background") ?? "", /var\(\s*--red-dim\s*\)/,
    "the measured backdrops below assume the panel is filled with --red-dim");
  const backdrop = { dark: parseColor("#31232b"), light: parseColor("#ffeceb") };

  let seen = 0;
  for (const rule of RULES.filter((r) => /^\.decision-btn-quiet(:|$)/.test(r.selector))) {
    const color = decl(rule.body, "color");
    if (!color) continue;
    const token = color.match(/var\(\s*(--[a-zA-Z0-9-]+)/)?.[1];
    assert.ok(token, `${rule.selector} must colour its label with a token`);
    assert.ok(!META_TOKENS.includes(token),
      `${rule.selector} uses ${token} — the dim meta grey is what made it read as disabled`);
    seen += 1;
    for (const theme of ["dark", "light"]) {
      const fg = resolveToken(token, theme);
      const ratio = contrast(fg, backdrop[theme]);
      assert.ok(ratio >= AA_SMALL,
        `${rule.selector} (${token} = ${show(fg)}) on the measured panel backdrop `
        + `${show(backdrop[theme])} in ${theme}: ${ratio.toFixed(2)}:1 < ${AA_SMALL}:1`);
    }
  }
  assert.ok(seen >= 1, "no .decision-btn-quiet colour rule found — has it been renamed?");
});
