import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

// Guards ONE bug class: a CSS variable that silently resolves to a hardcoded dark
// literal. It happens three ways, all invisible in dark mode and broken in light:
//   (a) styles.css READS a var it never defines → the var() fallback literal wins
//       (`var(--fg, #e6e9f0)` painted a headline number near-white on a white card);
//   (b) a token is defined ONLY in :root (dark) and never overridden for light → the
//       light theme reuses the dark hue (--accent-300 rendered the ACTIVE nav item
//       at 2.43:1, and the --c-* lane colours at 1.66–2.02:1);
//   (c) a SCOPED var (`--role-color`, set per role) has no rule for some role the UI
//       can actually render → that role falls back to the literal `#555`.

const SRC = dirname(fileURLToPath(import.meta.url));
const raw = readFileSync(join(SRC, "styles.css"), "utf8");
// Strip comments first: a commented-out `--x:` must not count as a definition, and
// a commented `var(--y)` must not count as a read.
const css = raw.replace(/\/\*[\s\S]*?\*\//g, "");

const jsx = readdirSync(SRC)
  .filter((f) => f.endsWith(".jsx"))
  .map((f) => readFileSync(join(SRC, f), "utf8"))
  .join("\n");
const config = readFileSync(join(SRC, "..", "tailwind.config.js"), "utf8");

const definedInCss = new Set([...css.matchAll(/(--[a-zA-Z0-9-]+)\s*:/g)].map((m) => m[1]));
const setFromJsx = new Set([...jsx.matchAll(/"(--[a-zA-Z0-9-]+)"\s*:/g)].map((m) => m[1]));
const readInCss = [...css.matchAll(/var\(\s*(--[a-zA-Z0-9-]+)\s*[,)]/g)].map((m) => m[1]);

// The light theme's own block, matched as a RULE. Matching by indexOf on the string
// `[data-theme="light"]` finds the file's header COMMENT first, which would make
// every "is it overridden for light?" assertion below a tautology that passes on
// the whole stylesheet.
const lightBlock = css.match(/\[data-theme="light"\]\s*\{([^}]*)\}/)?.[1];

test("styles.css defines every variable it reads (no dark-only literal fallbacks)", () => {
  const undefinedVars = [...new Set(readInCss)]
    .filter((v) => !definedInCss.has(v) && !setFromJsx.has(v))
    .sort();
  assert.deepEqual(
    undefinedVars,
    [],
    `styles.css reads variables it never defines: ${undefinedVars.join(", ")}. ` +
      "Each falls back to a hardcoded dark literal and breaks the light theme.",
  );
});

test("every colour token the Tailwind bridge maps is overridden for the light theme", () => {
  assert.ok(lightBlock, 'the [data-theme="light"] rule must exist');
  // Derived from tailwind.config.js rather than hand-copied: a hand-copied list
  // drifts silently the moment the bridge grows.
  const bridged = [...new Set([...config.matchAll(/var\(\s*(--[a-zA-Z0-9-]+)\s*\)/g)].map((m) => m[1]))];
  assert.ok(bridged.length >= 10, `expected the bridge to map tokens, found ${bridged.length}`);
  const fonts = new Set(["--font-mono", "--font-ui", "--font-display"]);
  for (const token of bridged) {
    assert.ok(definedInCss.has(token), `${token} is bridged but never defined`);
    // Fonts are intentionally theme-independent; colours are not.
    if (!fonts.has(token)) {
      assert.ok(
        lightBlock.includes(`${token}:`),
        `${token} is bridged but defined only in the dark :root — light reuses the dark value`,
      );
    }
  }
});

test("colour tokens read by themed UI are defined for BOTH themes", () => {
  // Each of these was dark-only and caused a measured light-theme contrast failure.
  for (const token of [
    "--accent-300",
    "--c-intake", "--c-context", "--c-building", "--c-review", "--c-testing",
    "--c-awaiting", "--c-done", "--c-escalated", "--c-answer",
    "--agent-planner", "--agent-watcher",
  ]) {
    assert.ok(definedInCss.has(token), `${token} must be defined`);
    assert.ok(
      lightBlock.includes(`${token}:`),
      `${token} must be overridden for the light theme (it is a colour, not a shape)`,
    );
  }
});

test("every role the UI can render has a --role-color rule (no #555 fallback)", () => {
  // eventRoles.js decides an event's role; styles.css colours it through a scoped
  // `--role-color`. A role with no rule falls back to the hardcoded literal #555.
  // The live DB proves `planner` (1,088 events) and `watcher` (291) are real sources.
  const roles = readFileSync(join(SRC, "eventRoles.js"), "utf8");
  const labelBlock = roles.match(/ROLE_LABEL\s*=\s*\{([^}]*)\}/)[1];
  const known = [...labelBlock.matchAll(/(\w+)\s*:/g)].map((m) => m[1]);
  assert.ok(known.includes("planner") && known.includes("watcher"), "sanity: roles parsed");

  for (const family of ["activity-event", "activity-event-rich", "activity-status-bar"]) {
    for (const role of known) {
      assert.match(
        css,
        new RegExp(`\\.${family}\\.role-${role}\\s*\\{[^}]*--role-color`),
        `.${family}.role-${role} has no --role-color rule — it falls back to the literal #555`,
      );
    }
  }
});
