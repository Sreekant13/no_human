import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

// Dead selectors are not just clutter — they hide gaps. The blocker panel's stylesheet
// read as though it covered one-click answer buttons long after the JSX stopped rendering
// them, which is part of how a missing keyboard-focus style next door survived review:
// the CSS looked more complete than the markup was.
//
// Scoped to the .blocker-* family on purpose. A repo-wide "unused class" rule would fire
// on classes set from .js maps, e2e selectors, and markdown-rendered HTML, and a test that
// cries wolf gets deleted. This family is fully static and hand-written.
//
// "Referenced" means referenced by MARKUP (.jsx). An e2e selector naming a class is not
// usage — a class only ever named by a test, and rendered by no component, is a stale
// assertion rather than a live style, and does not count as a reference here.

const SRC = dirname(fileURLToPath(import.meta.url));

function readAllJsx(dir) {
  let out = "";
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    // Recursive: the flat web/src layout is today's fact, not a guarantee. A component
    // moved into a subdirectory must not read as "referenced nowhere".
    if (e.isDirectory() && e.name !== "node_modules") out += readAllJsx(join(dir, e.name));
    else if (e.name.endsWith(".jsx")) out += readFileSync(join(dir, e.name), "utf8") + "\n";
  }
  return out;
}

// A class named only in a comment is not usage — "// TODO: bring back blocker-foo" would
// otherwise keep a dead rule alive forever. Line comments are matched only when not
// preceded by `:` so that https:// URLs survive.
const stripJsComments = (src) =>
  src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/[^\n]*/g, "$1");

const css = readFileSync(join(SRC, "styles.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
const jsx = stripJsComments(readAllJsx(SRC));

test("no .blocker-* rule outlives the JSX that used it", () => {
  const classes = [...new Set([...css.matchAll(/\.(blocker-[a-zA-Z0-9-]+)/g)].map((m) => m[1]))];
  assert.ok(
    classes.length > 5,
    `expected the blocker panel's classes, found ${classes.length} — did the family get ` +
      `renamed? If so, retarget this test rather than deleting it.`,
  );
  // `(?![\w-])` not `\b`: a word boundary is hyphen-permeable, so a mention of
  // `blocker-wake-extra` would read as a reference to `blocker-wake` and keep a dead rule
  // alive. The trailing guard has to exclude the hyphen the class names are built from.
  const orphans = classes.filter((c) => !new RegExp(`${c}(?![a-zA-Z0-9_-])`).test(jsx));
  assert.deepEqual(
    orphans,
    [],
    `dead CSS — these .blocker-* rules are referenced by no .jsx: ${orphans.join(", ")}`,
  );
});
