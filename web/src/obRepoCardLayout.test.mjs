import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

// Regression guard for the "Recently worked on" onboarding card: once a card
// is profiled, its status line used to wrap one word per line and its name/
// path collapsed to a ~40px sliver ("tiny-…", "/pri…") because the auto-fill
// grid track had nowhere to grow and the Prove panel claimed the whole `auto`
// column at max-content width. The fix gives an expanded card the whole grid
// row and makes the status/prove blocks full-width rows inside it — this test
// asserts those rules stay in place selector-shaped, not whitespace-exact, so
// a future stylesheet reflow does not cry wolf, but a re-collapse of the
// layout (e.g. someone "simplifying" the grid back to `1fr auto`) does.

const SRC = dirname(fileURLToPath(import.meta.url));
const raw = readFileSync(join(SRC, "styles.css"), "utf8");
const css = raw.replace(/\/\*[\s\S]*?\*\//g, "");

test("an expanded card (one that has a .ob-prove panel) spans the full grid row", () => {
  const rule = css.match(/\.ob-repo-card:has\(\s*\.ob-prove\s*\)\s*\{([^}]*)\}/);
  assert.ok(rule, "expected a `.ob-repo-card:has(.ob-prove) { ... }` rule");
  assert.match(
    rule[1],
    /grid-column\s*:\s*1\s*\/\s*-1/,
    "the :has(.ob-prove) rule must span the card across the whole grid row (grid-column: 1 / -1)",
  );
});

test(".ob-repo-card left column cannot collapse below its content", () => {
  const rule = css.match(/(?<!:has\([^)]*\)\s*)\.ob-repo-card\s*\{([^}]*)\}/);
  assert.ok(rule, "expected the base `.ob-repo-card { ... }` rule");
  assert.match(
    rule[1],
    /grid-template-columns\s*:\s*minmax\(\s*0\s*,\s*1fr\s*\)\s+auto/,
    "the left track must be minmax(0, 1fr), not a bare 1fr — a bare 1fr is what collapsed to ~40px",
  );
});

test(".ob-repo-card .ob-repo-status is a full-width single line, not the checkbox-row auto-margin layout", () => {
  const rule = css.match(/\.ob-repo-card\s+\.ob-repo-status\s*\{([^}]*)\}/);
  assert.ok(rule, "expected a card-scoped `.ob-repo-card .ob-repo-status { ... }` override");
  assert.match(rule[1], /white-space\s*:\s*nowrap/, "status must not wrap one word per line");
  assert.doesNotMatch(
    rule[1],
    /margin-left\s*:\s*auto/,
    "the card override must not re-declare the checkbox-row's margin-left:auto",
  );
});

test(".ob-repo-card .ob-prove overrides the checkbox-row indent to 0", () => {
  const rule = css.match(/\.ob-repo-card\s+\.ob-prove\s*\{([^}]*)\}/);
  assert.ok(rule, "expected a card-scoped `.ob-repo-card .ob-prove { ... }` override");
  assert.match(
    rule[1],
    /margin-left\s*:\s*0\b/,
    "the 30px checkbox-row indent (base .ob-prove rule) is meaningless inside a card and must be zeroed",
  );
});

test(".ob-repo-card-name and .ob-repo-card-path keep their end-ellipsis truncation", () => {
  for (const sel of [".ob-repo-card-name", ".ob-repo-card-path"]) {
    const escaped = sel.replace(/[.]/g, "\\.");
    const rule = css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
    assert.ok(rule, `expected a \`${sel} { ... }\` rule`);
    assert.match(rule[1], /text-overflow\s*:\s*ellipsis/, `${sel} must stay CSS end-ellipsis, not JS mid-truncation`);
    assert.match(rule[1], /white-space\s*:\s*nowrap/, `${sel} must stay single-line`);
  }
});

test(".ob-repo base checkbox-row rules are untouched (card-only overrides)", () => {
  // The un-expanded checkbox list (.ob-repo / .ob-repo-status without the
  // .ob-repo-card ancestor) is not the reported defect — its rule must still
  // exist standalone, unscoped to a card ancestor.
  const rule = css.match(/(?<!\.ob-repo-card\s)\.ob-repo-status\s*\{([^}]*)\}/);
  assert.ok(rule, "expected the base `.ob-repo-status { ... }` rule to still exist");
  assert.match(rule[1], /margin-left\s*:\s*auto/, "the checkbox-row status must keep its margin-left:auto layout");
});
