import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// The inline integration glyphs (integrationIcons.jsx). `node --test` has no
// JSX loader, so — like onboardingIntegrations.test.mjs — this reads the source
// text rather than importing the component.
//
// F9(b): Microsoft Teams had NO glyph in MARKS, so IntegrationIcon fell through
// to the plain-circle fallback and the Teams card showed a bare circle a real
// user called the "wrong icon". These pin that teams now has its own mark, in
// the same neutral-accent GENERIC style as every other glyph (not the vendor's
// logo — see the file header + TRADEMARK.md), and that it stays distinct from
// slack's single message bubble.

const here = fileURLToPath(new URL(".", import.meta.url));
const src = readFileSync(here + "integrationIcons.jsx", "utf8");

// The `name: (c) => ( … )` block for one glyph, so assertions can be scoped to it.
function markBlock(name) {
  const m = src.match(new RegExp(`\\n  ${name}: \\(c\\) => \\(([\\s\\S]*?)\\n  \\),`));
  return m ? m[1] : null;
}

test("teams has its own glyph in MARKS (so it never falls to the plain-circle fallback)", () => {
  const teams = markBlock("teams");
  assert.ok(teams, "MARKS must define a `teams` glyph");
  // ICON_NAMES is Object.keys(MARKS), so a teams entry puts teams in ICON_NAMES.
  assert.match(src, /export const ICON_NAMES = Object\.keys\(MARKS\);/);
});

test("the teams glyph paints in the shared neutral accent, not a vendor colour", () => {
  const teams = markBlock("teams");
  // Every path draws with the passed-in colour (currentColor === GLYPH_ACCENT).
  assert.match(teams, /stroke=\{c\}/, "the teams glyph must stroke with the shared accent");
  // No brand hex smuggled in — the whole point of the generic-glyph rule.
  assert.doesNotMatch(teams, /#[0-9a-fA-F]{3,6}/, "no per-vendor hex colour in the teams glyph");
  // It must not be a redraw of Microsoft's actual Teams mark — kept generic on
  // purpose. A comment saying so lives right above the glyph.
  assert.match(src, /NOT Microsoft's Teams logo/);
});

test("teams is a distinct glyph from slack (the two notification channels don't collide)", () => {
  const teams = markBlock("teams");
  const slack = markBlock("slack");
  assert.ok(teams && slack, "both marks must exist");
  assert.notEqual(teams.trim(), slack.trim(), "teams and slack must not share a glyph");
});
