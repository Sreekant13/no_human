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
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { NH_EXE_NAME, bundledNhPath } from "./server.mjs";
import { validateIco } from "../packaging/icoFromPng.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const pkg = JSON.parse(fs.readFileSync(path.join(here, "package.json"), "utf8"));
// `nhPackagedFiles`, not `build` — see the shadow-config test below for why the
// key was renamed. The list itself is unchanged.
const declared = new Set(pkg.nhPackagedFiles.files);

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
               // The persisted light/dark choice. An undeclared themeState.mjs
               // is ERR_MODULE_NOT_FOUND at launch in the packaged app, because
               // main.mjs imports it statically for the pre-paint colour.
               "themeState.mjs",
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
        `${entry} loads "${dep}" but nhPackagedFiles.files does not list it — it would be ` +
        `missing from app.asar and the packaged app would fail to launch`);
    }
  });
}

test("nhPackagedFiles.files lists only files that exist", () => {
  for (const f of pkg.nhPackagedFiles.files) {
    assert.ok(fs.existsSync(path.join(here, f)),
      `nhPackagedFiles.files lists missing ${f}`);
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

// Neither icon binary is committed any more (see packaging/derive-icons.mjs) —
// and electron-builder.config.cjs now REFUSES to load (process.exit(1)) if
// desktop/build/icon.ico, or icon.icns on darwin, is absent or older than the
// brand master. That check runs at import time, below, so it must be
// satisfied BEFORE the import — derive on demand, right here, rather than
// relying on whatever a prior build happened to leave on disk.
{
  const r = spawnSync(process.execPath,
    [path.join(here, "..", "packaging", "derive-icons.mjs")], { stdio: "inherit" });
  if (r.status !== 0) {
    throw new Error("packagedFiles.test.mjs: derive-icons.mjs failed to produce "
      + "fresh desktop icons; see its FAIL: output above");
  }
}

// The real electron-builder configuration. It moved out of package.json into a
// .cjs file because the signing decision has to branch on the environment, so
// these invariants must be read from the file the BUILD actually uses — a test
// still asserting against a config-shaped key in package.json would pass while
// the shipped config said something else entirely.
const builderConfig = await import("./electron-builder.config.cjs")
  .then((m) => m.default ?? m);

test("package.json holds no `build` key — electron-builder must never find a shadow config", () => {
  // THE TRAP, and why the key is called `nhPackagedFiles`.
  //
  // package.json needs to hold the `files` list (the config above sources it
  // from there, and the allowlist guard at the top of this file reads it) — but
  // under the name `build` that list IS a config as far as electron-builder is
  // concerned. It reads a `build` key out of package.json all by itself
  // whenever --config is absent, and that minimal key has no extraResources, no
  // win/mac blocks, no artifactName and no signing plan. It happened on
  // 2026-08-05: a bare `npx electron-builder --win` produced a default-named
  // installer 22 MB lighter than the real one — an app with no server in it —
  // and every step reported success.
  //
  // Fencing the npm scripts (asserted below) does not close it: `npx
  // electron-builder --win`, an IDE task and a future CI step all reach the
  // shadow without going through a script. Renaming the key removes the shadow
  // instead of fencing it — electron-builder now finds no config at all on that
  // path and fails outright rather than half-building.
  const raw = JSON.parse(fs.readFileSync(new URL("./package.json", import.meta.url)));
  assert.equal(raw.build, undefined,
    "a `build` key is back in desktop/package.json. electron-builder will use "
    + "it as a config whenever --config is absent and will ship a payload-less "
    + "installer with every step green. Keep the list under `nhPackagedFiles`.");
  assert.ok(Array.isArray(raw.nhPackagedFiles?.files) && raw.nhPackagedFiles.files.length > 0,
    "nhPackagedFiles.files is where the packaged allowlist lives now");
});

test("every dist script names the real config", () => {
  // Belt as well as braces. With no `build` key an unconfigured invocation now
  // fails instead of shadow-building, but --config is still what selects the
  // signing plan, extraResources and artifactName, so the sanctioned path must
  // keep passing it explicitly.
  const scripts = JSON.parse(fs.readFileSync(new URL("./package.json", import.meta.url))).scripts;
  const dist = Object.entries(scripts).filter(([name]) => name.startsWith("dist"));
  assert.ok(dist.length >= 2, "the dist scripts are the sanctioned build path; where did they go?");
  for (const [name, cmd] of dist) {
    for (const invocation of cmd.split("&&").map((s) => s.trim())
      .filter((s) => s.includes("electron-builder"))) {
      assert.match(invocation, /--config electron-builder\.config\.cjs/,
        `${name}: an electron-builder invocation without --config does not use the real config`);
    }
  }
});

test("the frozen server is actually shipped as extraResources", () => {
  // `files` is guarded above; the PAYLOAD was not. Deleting this block builds a
  // DMG that launches and can never start a server.
  const extra = builderConfig.extraResources ?? [];
  const server = extra.find((e) => (e.to ?? e) === "nh-server");
  assert.ok(server, `no nh-server in extraResources: ${JSON.stringify(extra)}`);
  assert.match(server.from ?? "", /packaging\/dist\/nh-server$/,
    "extraResources points somewhere the build script does not produce");
  // server.mjs must look for the binary at exactly that destination — keep the
  // two in step. Asserted BEHAVIOURALLY rather than by matching the literal
  // `"nh-server", "nh"` in server.mjs's source: the binary's basename is now
  // platform-dependent (nh vs nh.exe), so a source regex would either have to
  // be loosened until it proved nothing, or would fail on Windows while the
  // code was right. Building the destination and asking bundledNhPath to find
  // it tests the actual invariant — that resolution lands where extraResources
  // puts the payload — on whichever platform is running.
  const res = fs.mkdtempSync(path.join(os.tmpdir(), "nhres-"));
  const dir = path.join(res, server.to);
  fs.mkdirSync(dir, { recursive: true });
  const exe = path.join(dir, NH_EXE_NAME);
  fs.writeFileSync(exe, "");
  assert.equal(bundledNhPath(res), exe,
    "bundledNhPath no longer matches the extraResources destination");
  // And the name really is the one PyInstaller emits for this platform.
  assert.equal(NH_EXE_NAME, process.platform === "win32" ? "nh.exe" : "nh");
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
  assert.deepEqual(builderConfig.files, pkg.nhPackagedFiles.files,
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
// Operator decision D1 (2026-07-30): src/no_human/ci_gate/ - the glab/kubectl
// post-PR gate, built one deployment deep - does not ship. It stays in
// this repo for internal use, so nothing in the source tree signals the
// restriction; only the three build files below carry it, and each one is
// silent on its own when it drifts. A DMG built on 2026-07-30 shipped all five
// ci_gate modules as frozen bytecode.
const repoRoot = path.join(here, "..");
const readRepo = (rel) => fs.readFileSync(path.join(repoRoot, rel), "utf8");

// ci_gate is `drop` in EXPORT_CLASSIFICATION.txt, so this distribution does not
// carry the module - but it does carry this file. With no module present the
// exclusion guards below have nothing to guard, and the honest outcome is
// SKIPPED WITH A REASON: a pass would read as "the exclusions are verified",
// and a failure would claim something is wrong when nothing is. In the private
// source the directory exists, nothing is skipped, and every guard stays strict.
const ciGateInTree = fs.existsSync(path.join(repoRoot, "src", "no_human", "ci_gate"));
const ciGateDropped = ciGateInTree
  ? false
  : "src/no_human/ci_gate is not in this distribution (EXPORT_CLASSIFICATION: drop), " +
    "so there is no module for the packaging exclusions to exclude";

test("ci_gate still exists in the source tree, so the exclusions are live",
  { skip: ciGateDropped }, () => {
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
  // The icns half is derivable only on macOS (sips/iconutil —
  // derive-icons.mjs prints SKIP elsewhere), and build/ outputs are
  // untracked, so on a Linux CI runner the file CANNOT exist and its absence
  // proves nothing about the mac build (first public CI run, 2026-08-17).
  // On darwin absence stays a hard failure — that is where the mac build
  // happens and where the 2026-07-31 stock-atom regression would recur. If
  // the file exists anywhere, its bytes are validated regardless.
  if (process.platform !== "darwin" && !fs.existsSync(icns)) {
    return; // .ico parity test below still runs on every platform
  }
  assert.ok(fs.existsSync(icns), "desktop/build/icon.icns is missing");
  // icns magic bytes, so a truncated or mislabeled file cannot pass.
  const buf = fs.readFileSync(icns);
  assert.equal(buf.subarray(0, 4).toString("latin1"), "icns",
    "desktop/build/icon.icns is not an icns file");

  // ...and the SIZE TABLE, because magic bytes alone are blind to appearance.
  // The brand master ships at 512x512 only (web/public/nh-mark-512.png), so the
  // iconset derive-icons.mjs builds maxes out at NINE variants and deliberately
  // carries no 1024 (`ic10`) — upscaling 512 -> 1024 would add interpolation
  // artifacts, not real detail; see the header comment in
  // packaging/derive-icons.mjs for the one-line fix once a 1024px master
  // lands. Walking the TOC costs nothing and turns "is this an icns" into "is
  // this OUR icns, at every size a 512px master can honestly produce". It is
  // deliberately a table of OSTypes rather than a pixel assertion: this file
  // has no image decoder and must not grow one.
  //
  // It does NOT stand alone. Neither icon binary is committed any more — this
  // icns was freshly derived (above, before the config import) from
  // web/public/nh-mark-512.png by packaging/derive-icons.mjs, so this test is
  // really asserting that derivation itself, not a pinned artefact.
  const REQUIRED = ["ic04", "ic05", "ic07", "ic08", "ic09",
                    "ic11", "ic12", "ic13", "ic14"];
  const present = [];
  let off = 8;
  const total = buf.readUInt32BE(4);
  while (off + 8 <= Math.min(total, buf.length)) {
    const type = buf.toString("ascii", off, off + 4);
    const len = buf.readUInt32BE(off + 4);
    if (len < 8 || off + len > buf.length) break;
    present.push(type);
    off += len;
  }
  assert.ok(present.length >= 9,
    `the icns TOC walk found only ${present.length} entries - it did not parse`);
  const missing = REQUIRED.filter((t) => !present.includes(t));
  assert.deepEqual(missing, [],
    `desktop/build/icon.icns is missing size variant(s) ${missing.join(", ")}; ` +
    "the brand mark ships every size a 512px master can honestly produce");
});


// ---------------------------- the shipped documentation -------------------- #
//
// The bundle carried no user documentation at all, and the app's only route to
// any was a URL. That URL points at a page whose "Before you start" requires
// Python, uv, git and a checkout, and which never mentions a .dmg or the
// Applications folder — so for the packaged-app user, who is the ONLY person
// who can reach the Help menu, it documents a different install.
//
// These pin the config, not the built artefact: a build takes ~minutes and
// needs a full npm/electron install, so asserting the DECLARATION is what a
// unit test can honestly do. The artefact-level check is mounting the DMG.

test("the shipped docs are extraResources, not merely linked", () => {
  const extras = (builderConfig.mac?.extraResources
    || builderConfig.extraResources || []);
  const froms = extras.map((e) => (typeof e === "string" ? e : e.from));
  const quick = froms.find((f) => String(f).includes("docs/quickstart.md"));
  assert.ok(quick,
    `the quickstart is not in extraResources: ${JSON.stringify(froms)}. `
    + "Without it the Help menu has nothing offline to open and falls back to a "
    + "page written for a git checkout.");
  // EVERY doc the bundle ships, not just the one Help opens by default:
  // checking the quickstart alone let the second entry be dropped or misspelled
  // with the suite still green.
  const docs = froms.filter((f) => String(f).includes("/docs/"));
  for (const want of ["docs/quickstart.md", "docs/configuration.md"]) {
    assert.ok(docs.some((f) => String(f).endsWith(want)),
      `${want} is not in extraResources: ${JSON.stringify(froms)}`);
  }
  // The PATH must resolve, not merely be listed. A typo passes a
  // string-contains check and fails only at build time, on a machine that may
  // not be the one that made the typo. Only checked for docs/, which is always
  // in the repo — the other extraResources are build outputs (the frozen
  // server, electron's licences) and are legitimately absent in a worktree.
  for (const doc of docs) {
    assert.ok(fs.existsSync(path.resolve(here, doc)),
      `extraResources names ${doc}, which does not exist relative to desktop/`);
  }
});

test("main.mjs wires the Help handler and names the CANONICAL docs URL", () => {
  const src = fs.readFileSync(path.join(here, "main.mjs"), "utf8");
  // F5 from review: nothing asserted this wiring at all — misspelling DOCS_URL
  // or dropping onOpenDocs left every test green while the menu item died.
  assert.match(src, /onOpenDocs\s*:/,
    "buildAppMenu no longer passes onOpenDocs, so the Help menu is not built");
  assert.match(src, /getnohuman\.com\/docs(?!\.html)/,
    "the docs URL must be the canonical /docs — /docs.html only reaches it "
    + "through a 307, and the site's own markup links /docs everywhere");
  assert.doesNotMatch(src, /getnohuman\.com\/docs\.html/,
    "the non-canonical /docs.html form is back; it pins a redirect");
});

// ---------------------- mac / win parity (anti-drift) --------------------- //
//
// The Mac and Windows apps are ONE product. The realistic way they stop being
// one is not a deliberate decision — it is someone fixing a Windows problem by
// adding a `win.extraResources` or a Windows-only version, which works, ships,
// and silently gives the two platforms different payloads or different version
// numbers. These tests exist so that edit fails CI instead.

test("parity: extraResources is shared by both platforms, never per-platform", () => {
  // ONE mapping at the top level, inherited by mac and win alike. A
  // platform-scoped extraResources REPLACES the top-level list for that
  // platform, so the moment either block grows one, the two builds can ship
  // different payloads — the frozen server, the docs, or a licence notice could
  // be present on one platform and absent on the other with no build error.
  assert.ok(Array.isArray(builderConfig.extraResources),
    "extraResources must be declared once, at the top level");
  for (const p of ["mac", "win"]) {
    assert.equal(builderConfig[p]?.extraResources, undefined,
      `${p}.extraResources exists — it would OVERRIDE the shared list and let `
      + `${p} ship a different payload than the other platform`);
    assert.equal(builderConfig[p]?.files, undefined,
      `${p}.files exists — the asar allowlist must stay shared`);
  }
  // Both platforms are actually built, or "parity" is vacuous.
  assert.ok(Array.isArray(builderConfig.mac?.target) && builderConfig.mac.target.length);
  assert.ok(Array.isArray(builderConfig.win?.target) && builderConfig.win.target.length);
  // The payload both of them mount.
  const server = builderConfig.extraResources.find((e) => (e.to ?? e) === "nh-server");
  assert.ok(server, "the shared extraResources no longer carries the frozen server");
  assert.match(server.from ?? "", /packaging\/dist\/nh-server$/);
});

test("parity: the updater feed is emitted for both platforms", () => {
  // The mac header explains why `zip` is in mac.target: Squirrel.Mac updates
  // from a zip and electron-builder only writes latest-mac.yml when a zip
  // target exists. The Windows equivalent is nsis -> latest.yml. Losing either
  // leaves that platform's updater fetching a file no build produces.
  assert.ok(builderConfig.mac.target.includes("zip"),
    "mac lost its zip target — latest-mac.yml stops being emitted and the "
    + "updater fails with ERR_UPDATER_ZIP_FILE_NOT_FOUND");
  assert.ok(builderConfig.win.target.includes("nsis"),
    "win lost its nsis target — latest.yml stops being emitted");
  assert.ok(builderConfig.publish, "no publish block: neither feed is generated");
});

test("parity: one version source, with no platform-specific override", () => {
  // The NSIS exe must report the same version as the DMG. Both take it from
  // desktop/package.json, so the failure mode is not a mismatch today but a
  // Windows-only override added later.
  assert.match(pkg.version, /^\d+\.\d+\.\d+/, "package.json has no usable version");
  for (const p of ["mac", "win"]) {
    for (const key of ["version", "buildVersion"]) {
      assert.equal(builderConfig[p]?.[key], undefined,
        `${p}.${key} is set — that is a platform-specific version source and `
        + "the two installers would report different versions");
    }
  }
  assert.equal(builderConfig.extraMetadata?.version, undefined,
    "extraMetadata.version overrides the packaged version for BOTH platforms "
    + "and detaches it from package.json");
});

test("parity: the app version and the frozen server's version cannot drift", () => {
  // `nh --version` comes from pyproject; the shell's version comes from
  // package.json. They ship in ONE artifact, so a user reading either must see
  // the same number. Bumping one and forgetting the other is the drift.
  const pyproject = fs.readFileSync(path.join(here, "..", "pyproject.toml"), "utf8");
  // The [project] table's own version, not a dependency pin further down.
  const m = pyproject.match(/^\s*version\s*=\s*["']([^"']+)["']/m);
  assert.ok(m, "no version found in pyproject.toml");
  assert.equal(m[1], pkg.version,
    `pyproject version ${m[1]} != desktop/package.json version ${pkg.version} — `
    + "the installer and the `nh` inside it would report different versions");
});

test("parity: the Windows build has an icon, and it is a real .ico", () => {
  // Without this electron-builder falls back to Electron's stock atom icon, the
  // exact defect the mac icon.icns comment records. Checked as BYTES, not by
  // existence: a 0-byte or PNG-named-.ico placeholder would satisfy a path check
  // and still produce a broken installer.
  const icon = builderConfig.win?.icon;
  assert.ok(icon, "win.icon is unset — the installer would wear Electron's icon");
  const p = path.join(here, icon);
  assert.ok(fs.existsSync(p), `win.icon points at a file that does not exist: ${icon}`);
  const buf = fs.readFileSync(p);
  // ICONDIR: reserved=0, type=1, count>=1.
  assert.equal(buf.readUInt16LE(0), 0, `${icon} is not an ICO (reserved != 0)`);
  assert.equal(buf.readUInt16LE(2), 1, `${icon} is not an ICO (type != 1)`);
  const count = buf.readUInt16LE(4);
  assert.ok(count >= 1, `${icon} declares no images`);
  // NSIS needs a 256x256 entry; 256 is encoded as 0 in the single-byte field.
  const sizes = [];
  for (let i = 0; i < count; i++) {
    const w = buf[6 + i * 16];
    sizes.push(w === 0 ? 256 : w);
  }
  assert.ok(sizes.includes(256),
    `${icon} has no 256x256 entry (has ${sizes.join(", ")}) — NSIS requires one`);

  // derive-icons.mjs's contract (ported from the old make-win-icon.ps1): a
  // fixed 6-entry PNG-payload ICO with the directory laid out before every
  // payload, so the first payload always starts at 6 + 16*6 = 102. This is
  // the actual output-format guarantee the derivation tests pin from the
  // producer side (packaging/icoFromPng.mjs); asserting it here too catches
  // a mismatch between what derive-icons.mjs writes and what this build
  // config actually ships.
  assert.doesNotThrow(() => validateIco(buf),
    `${icon} failed packaging/icoFromPng.mjs's own validateIco()`);
  assert.equal(count, 6, `${icon} must have exactly 6 entries, got ${count}`);
  const firstOffset = buf.readUInt32LE(6 + 12);
  assert.equal(firstOffset, 102,
    `${icon}'s first payload must start at offset 102 (directory-first layout), got ${firstOffset}`);
});
