import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

// Guards ONE bug class: `npm test` running FEWER files than exist, silently.
//
// It has already happened twice, in opposite directions, and both times the job
// was red-or-green for a reason unrelated to the code under test:
//
//   * `node --test src/`  — a directory argument. Node 20 walks it (910 tests);
//     from Node 22 it is resolved as a MODULE and the run dies with
//     "Cannot find module .../web/src".
//   * `node --test "src/**/*.test.mjs"` — a quoted glob, expanded by Node. That
//     support does not exist in the Node 20 line, where the pattern is taken as
//     a literal path: "Could not find .../web/src/**/*.test.mjs", exit 1, ZERO
//     tests executed. A command that runs nothing and fails looks the same as a
//     command that runs everything and fails.
//
// The script is now an explicit file list (`node --test src/*.test.mjs`), which
// both lines run identically — but a file list is only as complete as the
// pattern that builds it, and a single-level `*` cannot see into a
// subdirectory. Nothing else in the tree would notice a test file added at
// `src/foo/bar.test.mjs`: it would simply never run, and the suite would stay
// green.
//
// So this reads the SHIPPED script out of package.json, expands its arguments
// the way the shell npm runs it would, and compares that against a recursive
// walk of src/. It does not check how the script is SPELLED — any spelling
// whose expansion covers every test file passes, and any spelling that misses
// one fails, including the two forms above.

const HERE = dirname(fileURLToPath(import.meta.url));
const WEB_ROOT = dirname(HERE);

/** Every *.test.mjs under `dir`, recursively, as paths relative to WEB_ROOT. */
function walkTests(dir, prefix, out = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const rel = `${prefix}/${entry.name}`;
    if (entry.isDirectory()) walkTests(join(dir, entry.name), rel, out);
    else if (entry.name.endsWith(".test.mjs")) out.push(rel);
  }
  return out;
}

/**
 * Expand one shell-style pattern, POSIX `sh` rules: `*` matches within a single
 * path segment and never crosses `/`. Deliberately NOT globstar — `sh` is not
 * globstar either unless a shell option is set, so a `**` in the script must
 * come out here exactly as poorly as it comes out in CI.
 */
function expand(pattern) {
  let matches = [""];
  for (const segment of pattern.split("/")) {
    if (!segment.includes("*")) {
      matches = matches.map((m) => (m ? `${m}/${segment}` : segment));
      continue;
    }
    const re = new RegExp(
      `^${segment.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, "[^/]*")}$`);
    const next = [];
    for (const m of matches) {
      let entries;
      try {
        entries = readdirSync(m ? join(WEB_ROOT, m) : WEB_ROOT);
      } catch {
        continue; // a non-directory on the way down matches nothing, as in sh
      }
      for (const name of entries.sort()) {
        if (re.test(name)) next.push(m ? `${m}/${name}` : name);
      }
    }
    matches = next;
  }
  return matches;
}

/** The path arguments of the `test` script, as the shell would hand them over. */
function testScriptTargets() {
  const pkg = JSON.parse(
    readFileSync(join(WEB_ROOT, "package.json"), "utf8"));
  const words = pkg.scripts.test.trim().split(/\s+/);
  const flag = words.indexOf("--test");
  assert.notEqual(flag, -1, "the test script no longer runs node --test");
  return words.slice(flag + 1)
    // The shell strips the quotes before node sees the word; a quoted glob is
    // therefore still a glob HERE, and still fails to expand on Node 20.
    .map((w) => w.replace(/^["']|["']$/g, ""))
    .filter((w) => !w.startsWith("-"));
}

test("npm test runs every *.test.mjs under web/src — no file is silently skipped", () => {
  const onDisk = walkTests(join(WEB_ROOT, "src"), "src").sort();

  // Fail closed. If the walk finds nothing the comparison below is vacuous and
  // would pass against a script that runs nothing at all.
  assert.ok(onDisk.length > 50,
    `the walk found only ${onDisk.length} test files — it is scanning the wrong tree`);
  assert.ok(onDisk.includes("src/testScriptCoverage.test.mjs"),
    "the walk cannot even see this file");

  const targets = testScriptTargets();
  assert.ok(targets.length > 0, "the test script passes no path to node --test");

  const expanded = [...new Set(targets.flatMap(expand))].sort();

  assert.deepEqual(expanded, onDisk,
    "the `test` script in web/package.json does not expand to exactly the test "
    + "files on disk. Files on disk that the script misses would never run and "
    + "the suite would stay green; paths the script names that do not exist "
    + "make node exit 1 having run nothing.");
});

test("the expander agrees with sh, so the comparison above means something", () => {
  // A single-level `*` must not cross a directory boundary, or the check above
  // would accept `src/*.test.mjs` while a nested file went unrun.
  assert.deepEqual(expand("src/assets/*.test.mjs"), [],
    "src/assets holds no test files — expanding to anything means the matcher is wrong");

  // The two broken historical forms must both come out as NOT covering the tree.
  const onDisk = walkTests(join(WEB_ROOT, "src"), "src").sort();
  assert.notDeepEqual(expand("src/"), onDisk,
    "a bare directory argument must not read as full coverage");
  assert.notDeepEqual(expand("src/**/*.test.mjs"), onDisk,
    "a globstar pattern must not read as full coverage: sh does not expand it "
    + "and the Node 20 line does not either");

  // And a pattern that DOES cover the tree must be recognised as covering it,
  // or the primary assertion could never pass for any spelling.
  assert.deepEqual(expand("src/*.test.mjs"), onDisk);
});
