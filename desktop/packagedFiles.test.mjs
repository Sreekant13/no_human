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
               // The update flow. main.mjs also READS package.json at runtime
               // (packagedSigning) — an undeclared package.json would make
               // every packaged build read as unsigned and silently disable
               // updates, which is a failure nothing else would catch.
               "updater.mjs", "updatePolicy.mjs", "updateState.mjs", "package.json",
               "server.mjs", "error.html", "token.html"],
  // preload.cjs requires package.json for the app version it hands the board.
  "preload.cjs": ["package.json"],
  "updater.mjs": ["updatePolicy.mjs"],
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

// The real electron-builder configuration. It moved out of package.json into a
// .cjs file because the signing decision has to branch on the environment, so
// these invariants must be read from the file the BUILD actually uses — a test
// still asserting against package.json.build would pass while the shipped
// config said something else entirely.
const builderConfig = await import("./electron-builder.config.cjs")
  .then((m) => m.default ?? m);

test("the frozen server is actually shipped as extraResources", () => {
  // `files` is guarded above; the PAYLOAD was not. Deleting this block builds a
  // DMG that launches and can never start a server.
  const extra = builderConfig.extraResources ?? [];
  const server = extra.find((e) => (e.to ?? e) === "nh-server");
  assert.ok(server, `no nh-server in extraResources: ${JSON.stringify(extra)}`);
  assert.match(server.from ?? "", /packaging\/dist\/nh-server$/,
    "extraResources points somewhere the build script does not produce");
  // server.mjs looks for it at Resources/nh-server/nh — keep the two in step.
  const srv = fs.readFileSync(new URL("./server.mjs", import.meta.url), "utf8");
  assert.match(srv, /"nh-server",\s*"nh"/,
    "bundledNhPath no longer matches the extraResources destination");
});

test("the MIT licence text ships inside the app bundle", () => {
  // The DMG carries only the .app; MIT asks for the licence text to travel
  // with substantial copies, so it rides as an extraResource.
  const extra = builderConfig.extraResources ?? [];
  const lic = extra.find((e) => (e.to ?? e) === "LICENSE");
  assert.ok(lic, `no LICENSE in extraResources: ${JSON.stringify(extra)}`);
  const src = fs.readFileSync(path.join(here, lic.from), "utf8");
  assert.match(src, /MIT License/, "extraResources LICENSE is not the MIT text");
});

test("Electron's and Chromium's notices ship too, or the DMG is undistributable", () => {
  // THE_DEFECT: this file guarded only our own MIT `LICENSE` entry, so the
  // build shipped for months with no notice for Electron (MIT) or Chromium
  // (BSD) at all — verified absent on desktop/dist/mac-arm64/no_human.app.
  // Chromium's BSD terms require the notice to be reproduced with a binary
  // distribution, so this is a redistribution blocker, not tidiness.
  //
  // Why they vanish, since the comment in the config was wrong once already:
  // electron-builder DELETES them. electronMac.js:219-220 unlinks
  // `appOutDir/LICENSE` and `appOutDir/LICENSES.chromium.html` (appOutDir is
  // dist/mac-arm64/, one level ABOVE the .app), and ElectronFramework.js:236-239
  // performs the `LICENSE -> LICENSE.electron.txt` rename that would have
  // preserved Electron's only when the platform is NOT macOS. Nothing was ever
  // overwritten: Electron's notices sit beside Electron.app in
  // node_modules/electron/dist/, and Contents/Resources has never held a file
  // named LICENSE.
  const extra = builderConfig.extraResources ?? [];
  const required = [
    { to: "LICENSE.electron.txt",
      from: /node_modules\/electron\/dist\/LICENSE$/,
      text: /Copyright \(c\) [\d\-, ]*GitHub Inc\./ },
    { to: "LICENSES.chromium.html",
      from: /node_modules\/electron\/dist\/LICENSES\.chromium\.html$/,
      text: /Chromium/ },
  ];
  for (const want of required) {
    const entry = extra.find((e) => (e.to ?? e) === want.to);
    assert.ok(entry, `no ${want.to} in extraResources: ${JSON.stringify(extra)} `
      + "— the bundle would ship without a third-party notice it must carry");
    assert.match(entry.from ?? "", want.from,
      `${want.to} is sourced from somewhere other than electron's dist`);
    // Content check only where node_modules is present. Coder worktrees do not
    // install it, and a hard read there would fail this test for a reason that
    // has nothing to do with the invariant. The two assertions above are
    // unconditional, so the test can still fail everywhere it runs.
    const src = path.join(here, entry.from);
    if (fs.existsSync(src)) {
      assert.match(fs.readFileSync(src, "utf8").slice(0, 4000), want.text,
        `${entry.from} does not look like the notice it claims to be`);
    }
  }

  // The destination names must not collide with our own MIT LICENSE, which is
  // the whole reason they are renamed rather than shipped under their own.
  const dests = extra.map((e) => e.to ?? e);
  assert.equal(new Set(dests).size, dests.length,
    `extraResources has colliding destinations: ${JSON.stringify(dests)} — a `
    + "later entry silently replaces an earlier one at the same path");

  // And the notices file that tells a distributor all this must name them, so
  // the obligation and the mechanism cannot drift apart again.
  const notices = fs.readFileSync(
    path.join(here, "..", "THIRD-PARTY-NOTICES.md"), "utf8");
  for (const want of required) {
    assert.ok(notices.includes(want.to),
      `THIRD-PARTY-NOTICES.md does not mention ${want.to}`);
  }
});

test("the mac targets include zip, or auto-update cannot work at all", () => {
  // Squirrel.Mac updates from a ZIP. electron-builder only emits
  // latest-mac.yml — the file electron-updater fetches — when a zip target is
  // present, so `["dmg"]` alone produces a build whose updater fails at
  // runtime with ERR_UPDATER_ZIP_FILE_NOT_FOUND. Nothing else in the suite
  // would notice: the DMG builds, launches, and simply never updates.
  const targets = builderConfig.mac?.target ?? [];
  assert.ok(targets.includes("zip"),
    `mac.target must include "zip" for the update feed, got ${JSON.stringify(targets)}`);
});

test("the build config and the packaging guard read the SAME file list", () => {
  // The allowlist above is only meaningful if it is the list the build uses.
  assert.deepEqual(builderConfig.files, pkg.build.files,
    "electron-builder.config.cjs must source `files` from package.json, or the "
    + "guard protects a list nobody ships");
});

test("the signing verdict is stamped into the packaged app", () => {
  // main.mjs decides whether it may auto-update by reading these from its own
  // package.json. Dropping extraMetadata makes every build read as unsigned —
  // updates silently off, forever, with no error anywhere.
  const meta = builderConfig.extraMetadata ?? {};
  assert.ok(Object.hasOwn(meta, "nhSigning"),
    "the build must record which signing mode produced it");
  assert.ok(Object.hasOwn(meta, "nhCanAutoUpdate"),
    "the build must record whether the shipped app may update itself");
  // This suite runs without signing credentials, so the honest answer is no.
  assert.equal(meta.nhCanAutoUpdate, false,
    "an unsigned CI build must not claim it can auto-update");
});

test("a publish provider exists so latest-mac.yml is generated", () => {
  // Without a publish block electron-builder writes no update metadata, and
  // the updater has nothing to fetch. `--publish never` on every script is
  // what prevents an actual upload — this block only says where to LOOK.
  const publish = builderConfig.publish ?? [];
  assert.ok(publish.length > 0, "no publish provider — no update feed");
  assert.equal(publish[0].provider, "github");
});

test("the version handed to the board is the real one, not npm_package_version", () => {
  // `npm_package_version` is set only by `npm run`, so in every packaged DMG
  // this read was the literal string "dev". That is now load-bearing: the
  // update UI compares it against the released version, and "dev" parses as
  // nothing, so a stale install would never learn it was stale. This is a
  // build-config invariant (like the rest of this file) because preload.cjs
  // cannot be imported without a real Electron renderer.
  const preload = fs.readFileSync(new URL("./preload.cjs", import.meta.url), "utf8");
  assert.match(preload, /require\(["']\.\/package\.json["']\)/,
    "preload.cjs must read the packaged package.json for the app version");
  const versionLine = preload.match(/version:\s*(.+),/)?.[1] ?? "";
  assert.doesNotMatch(versionLine, /npm_package_version/,
    "the exposed version must not come straight from npm_package_version");
});

test("no script publishes anything", () => {
  // Publishing is the operator's call, and an accidental `--publish always`
  // would push a release from a developer machine.
  for (const [name, script] of Object.entries(pkg.scripts)) {
    if (!script.includes("electron-builder")) continue;
    assert.ok(script.includes("--publish never"),
      `script "${name}" runs electron-builder without --publish never`);
  }
});

// --------------------------------------------------------------------------
// Operator decision D1 (2026-07-30): src/no_human/ci_gate/ - the glab/kubectl/
// metrics-core post-PR gate built one customer deep - does not ship. It stays in
// this repo for internal use, so nothing in the source tree signals the
// restriction; only the three build files below carry it, and each one is
// silent on its own when it drifts. A DMG built on 2026-07-30 shipped all five
// ci_gate modules as frozen bytecode.
const repoRoot = path.join(here, "..");
const readRepo = (rel) => fs.readFileSync(path.join(repoRoot, rel), "utf8");

test("ci_gate still exists in the source tree, so the exclusions are live", () => {
  // Guard the guard: once ci_gate leaves the tree the three tests below assert
  // nothing, and a green run would mean the opposite of what it looks like.
  assert.ok(fs.existsSync(path.join(repoRoot, "src", "no_human", "ci_gate")),
    "src/no_human/ci_gate is gone - the exclusion guards below now pass " +
    "vacuously; retire them along with the module");
});

test("the PyInstaller spec excludes no_human.ci_gate from the freeze", () => {
  const spec = readRepo("packaging/nh-server.spec");
  const m = spec.match(/excludes\s*=\s*\[([\s\S]*?)\]/);
  assert.ok(m, "nh-server.spec has no excludes= list - this guard has gone blind");
  assert.match(m[1], /["']no_human\.ci_gate["']/,
    "nh-server.spec no longer excludes no_human.ci_gate - PyInstaller follows " +
    "the lazy import in blockers/wake.py and freezes the package into the PYZ " +
    "the DMG ships");
});

test("the PyInstaller spec excludes the private term inventory from the freeze", () => {
  // The private half of the publish guard's term list. Its docstring forbids
  // publication in any form; vendor_terms.py has an empty-fallback import for
  // exactly this absence. It was frozen into every DMG until 2026-07-31.
  const spec = readRepo("packaging/nh-server.spec");
  const m = spec.match(/excludes\s*=\s*\[([\s\S]*?)\]/);
  assert.ok(m, "nh-server.spec has no excludes= list - this guard has gone blind");
  assert.match(m[1], /["']no_human\.eval\._vendor_terms_private["']/,
    "nh-server.spec no longer excludes no_human.eval._vendor_terms_private - " +
    "the hex-encoded private inventory freezes into the PYZ the DMG ships");
  // And the build script's fail-closed check for the same module.
  const installer = readRepo("packaging/build-installer.sh");
  assert.match(installer, /_vendor_terms_private/,
    "build-installer.sh no longer refuses a bundle carrying the inventory");
});

test("the wheel build excludes src/no_human/ci_gate", () => {
  const toml = readRepo("pyproject.toml");
  const table = toml.split(/^\[/m)
    .find((t) => t.startsWith("tool.hatch.build.targets.wheel]"));
  assert.ok(table, "pyproject.toml has no [tool.hatch.build.targets.wheel] table");
  assert.match(table, /exclude\s*=\s*\[[^\]]*["']src\/no_human\/ci_gate["']/,
    "the wheel target no longer excludes src/no_human/ci_gate - packages = " +
    '["src/no_human"] takes the whole tree, ci_gate included');
});

test("the installer build fails when ci_gate reaches the frozen output", () => {
  const sh = readRepo("packaging/build-installer.sh");
  const at = sh.indexOf("FAIL: no_human.ci_gate is frozen into the bundle");
  assert.ok(at > 0,
    "build-installer.sh no longer fails the build on a frozen ci_gate - the " +
    "spec exclude becomes an unchecked claim");
  assert.match(sh.slice(at, at + 300), /exit 1/,
    "the ci_gate check reports but does not stop the build");
  assert.match(sh, /PYZ-\*\.toc/,
    "the check must read PyInstaller's own module table - the PYZ is " +
    "zlib-compressed, so searching the bundle for the name reads clean while " +
    "the module sits inside it");
});

test("the app ships the brand icon, not Electron's stock atom", () => {
  // electron-builder reads buildResources/icon.icns for the mac target; if
  // either half goes missing the build silently falls back to the Electron
  // default icon — which is exactly what shipped until 2026-07-31.
  assert.equal(builderConfig.directories.buildResources, "build",
    "buildResources no longer points at desktop/build");
  const icns = path.join(here, "build", "icon.icns");
  assert.ok(fs.existsSync(icns), "desktop/build/icon.icns is missing");
  // icns magic bytes, so a truncated or mislabeled file cannot pass.
  const head = fs.readFileSync(icns).subarray(0, 4).toString("latin1");
  assert.equal(head, "icns", "desktop/build/icon.icns is not an icns file");
});
