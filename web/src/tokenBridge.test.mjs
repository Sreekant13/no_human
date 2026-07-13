import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import config from "../tailwind.config.js";

// Task 5.0 Step 1: the token bridge maps Tailwind color/font utilities to the
// SAME CSS variables the plain-CSS screens use, so light/dark stay driven by the
// [data-theme] blocks in styles.css — one source of truth. The CLI emits e.g.
// `.bg-card{background-color:var(--bg-card)}` (proven in the spike); these tests
// pin the config that drives it AND that every target var actually exists.

const here = fileURLToPath(new URL(".", import.meta.url));
const stylesCss = readFileSync(here + "styles.css", "utf8");
const mainJsx = readFileSync(here + "main.jsx", "utf8");

const colors = config.theme.extend.colors;
const fonts = config.theme.extend.fontFamily;

test("colors bridge to the existing CSS vars", () => {
  assert.equal(colors.base, "var(--base)");
  assert.equal(colors.card, "var(--bg-card)");
  assert.equal(colors.panel, "var(--bg-panel)");
  assert.equal(colors.hover, "var(--bg-hover)");
  assert.equal(colors.line, "var(--border)"); // 'border' collides with border utils
  assert.equal(colors.text, "var(--text)");
  assert.equal(colors["text-hi"], "var(--text-hi)");
  assert.equal(colors["text-muted"], "var(--text-muted)");
  assert.equal(colors["text-dim"], "var(--text-dim)");
  assert.equal(colors.accent, "var(--accent-500)");
  assert.equal(colors["accent-600"], "var(--accent-600)");
});

test("fonts bridge to the existing font vars", () => {
  assert.equal(fonts.mono, "var(--font-mono)");
  assert.equal(fonts.ui, "var(--font-ui)");
  assert.equal(fonts.display, "var(--font-display)");
});

test("every bridged var is actually DEFINED in styles.css (else the utility "
  + "resolves to nothing)", () => {
  const targets = [...Object.values(colors), ...Object.values(fonts)];
  for (const t of targets) {
    const name = t.match(/^var\((--[a-z0-9-]+)\)$/)?.[1];
    assert.ok(name, `bridged value ${t} is not a var(--…) reference`);
    assert.match(stylesCss, new RegExp(`${name}\\s*:`),
      `${name} (from bridge) is not defined in styles.css`);
  }
});

test("Preflight stays OFF — the Step-0 spike proved OFF is pixel-stable vs the "
  + "no-Tailwind baseline (Board+Stats, light+dark); ON altered an un-migrated "
  + "screen. Do not flip without re-running the spike.", () => {
  assert.equal(config.corePlugins.preflight, false);
});

test("tailwind.css is imported BEFORE styles.css — the ONLY thing that keeps "
  + "un-migrated screens pixel-stable (v3 emits flat CSS, source order decides).", () => {
  const twIdx = mainJsx.indexOf('"./tailwind.css"');
  const cssIdx = mainJsx.indexOf('"./styles.css"');
  assert.ok(twIdx >= 0, "main.jsx must import ./tailwind.css");
  assert.ok(cssIdx >= 0, "main.jsx must import ./styles.css");
  assert.ok(twIdx < cssIdx, "tailwind.css must be imported before styles.css");
});
