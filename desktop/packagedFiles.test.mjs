// electron-builder's `files` is a literal ALLOWLIST: anything absent is simply
// missing from the asar, with no build error. main.mjs imports its siblings
// statically, so an omission is a launch-time ERR_MODULE_NOT_FOUND — the app
// opens to nothing. That shipped once (token.html, tokenStore.mjs, badge.mjs
// and menu.mjs were all absent from a built app.asar), so it is guarded here.
//
// This asserts a build-config invariant, not UI behaviour: every local file the
// main process loads at runtime must be declared.
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const pkg = JSON.parse(fs.readFileSync(path.join(here, "package.json"), "utf8"));
const declared = new Set(pkg.build.files);

/**
 * Sibling files referenced by static import, dynamic import, require, or
 * loadFile(join(__dirname, …)) — either quote style. Only `./` siblings are
 * checked: a `..` path leaves the packaged directory and is not governed by
 * `build.files` (matching it produced a false failure on `path.join(__dirname,
 * "..", "web")`).
 */
function referenced(file) {
  const src = fs.readFileSync(path.join(here, file), "utf8");
  const out = new Set();
  const patterns = [
    /(?:from|import)\s*\(?\s*["']\.\/([^"']+)["']/g,   // static + dynamic import
    /require\s*\(\s*["']\.\/([^"']+)["']/g,            // CJS require
    /new\s+URL\s*\(\s*["']\.\/([^"']+)["']/g,         // new URL("./x", import.meta.url)
  ];
  for (const re of patterns) {
    for (const m of src.matchAll(re)) out.add(m[1]);
  }
  // join(__dirname, "a", "b", …) — take ALL segments, not just the first, or a
  // nested path is reported as its directory name and false-fails.
  for (const m of src.matchAll(/__dirname\s*,\s*((?:\s*["'][^"']+["']\s*,?)+)/g)) {
    const segs = [...m[1].matchAll(/["']([^"']+)["']/g)].map((x) => x[1]);
    // A path that climbs out of this directory is not governed by build.files.
    if (segs.some((seg) => seg === "..")) continue;
    out.add(segs.join("/"));
  }
  return out;
}

// What each entry is KNOWN to load. An explicit list, not a count: a threshold
// tolerates silently losing deps, and `preload.cjs` has none today, so a bare
// loop over it asserted nothing at all.
const EXPECTED = {
  "main.mjs": ["tokenStore.mjs", "setupGate.mjs", "serverOwnership.mjs",
               "serverLifecycle.mjs", "quitPolicy.mjs", "navScheduler.mjs", "badge.mjs", "menu.mjs",
               // main.mjs took a direct dependency on setupUi.mjs for the
               // failed-restart copy; omitting it here left the blindness guard
               // itself partly blind.
               "setupUi.mjs",
               "server.mjs", "error.html", "token.html"],
  "preload.cjs": [],
  // HTML pages load ES modules too: token.html imports setupUi.mjs, and an
  // undeclared one is a blank screen in the packaged app.
  "token.html": ["setupUi.mjs"],
  "error.html": [],
  // server.mjs imports serverOwnership.mjs, so it is load-bearing twice over.
  "server.mjs": ["serverOwnership.mjs"],
  "serverLifecycle.mjs": ["serverOwnership.mjs"],
};

for (const entry of Object.keys(EXPECTED)) {
  test(`every local file ${entry} loads is declared in build.files`, () => {
    const deps = referenced(entry);
    // Guard the guard: if detection silently stops seeing these, the
    // assertions below become vacuous and protect nothing.
    for (const known of EXPECTED[entry]) {
      assert.ok(deps.has(known),
        `${entry} loads "${known}" but referenced() no longer detects it — ` +
        `the packaging guard has gone blind`);
    }
    for (const dep of deps) {
      assert.ok(fs.existsSync(path.join(here, dep)), `${dep} should exist on disk`);
      assert.ok(declared.has(dep),
        `${entry} loads "${dep}" but build.files does not list it — it would be ` +
        `missing from app.asar and the packaged app would fail to launch`);
    }
  });
}

test("build.files lists only files that exist", () => {
  for (const f of pkg.build.files) {
    assert.ok(fs.existsSync(path.join(here, f)), `build.files lists missing ${f}`);
  }
});

test("both shell pages declare a language (WCAG 3.1.1)", () => {
  // Without lang, a screen reader announces "sk-ant-oat", "~/.no_human/.env"
  // and "nh start --no-open" in the user's default voice.
  for (const page of ["token.html", "error.html"]) {
    const html = fs.readFileSync(new URL(`./${page}`, import.meta.url), "utf8");
    assert.match(html, /<html\s+lang="[a-z]{2}(-[A-Za-z]+)?"/,
      `${page} must declare a language on its root element`);
  }
});

test("the frozen server is actually shipped as extraResources", () => {
  // `files` is guarded above; the PAYLOAD was not. Deleting this block builds a
  // DMG that launches and can never start a server.
  const pkg = JSON.parse(fs.readFileSync(
    new URL("./package.json", import.meta.url), "utf8"));
  const extra = pkg.build?.extraResources ?? [];
  const server = extra.find((e) => (e.to ?? e) === "nh-server");
  assert.ok(server, `no nh-server in extraResources: ${JSON.stringify(extra)}`);
  assert.match(server.from ?? "", /packaging\/dist\/nh-server$/,
    "extraResources points somewhere the build script does not produce");
  // server.mjs looks for it at Resources/nh-server/nh — keep the two in step.
  const srv = fs.readFileSync(new URL("./server.mjs", import.meta.url), "utf8");
  assert.match(srv, /"nh-server",\s*"nh"/,
    "bundledNhPath no longer matches the extraResources destination");
});
