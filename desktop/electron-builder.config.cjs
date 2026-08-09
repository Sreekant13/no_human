// electron-builder config as CODE, because the signing decision depends on the
// environment and package.json cannot branch.
//
// What this file is responsible for:
//
//   1. Signing when credentials exist, and NOT signing when they don't —
//      without a placeholder certificate name that could ship.
//   2. Saying loudly, in the build output, which of those happened.
//   3. Stamping the answer INTO the packaged app (`extraMetadata`), because
//      `process.env.CSC_LINK` means nothing at runtime inside a shipped DMG.
//      Without this the app would have to guess whether it may auto-update,
//      and guessing wrong means offering an install macOS will refuse.
//
// The `mac.target` list is not cosmetic: Squirrel.Mac updates from a ZIP, and
// electron-builder only emits `latest-mac.yml` — the file electron-updater
// fetches — when a zip target is present. With `["dmg"]` alone the updater
// fails at runtime with ERR_UPDATER_ZIP_FILE_NOT_FOUND. The DMG remains the
// thing a human downloads; the ZIP is what the updater consumes.

const { signingBanner, signingPlan } = require("./signing.cjs");

const plan = signingPlan(process.env);

// Printed on EVERY build, signed or not. The failure mode the operator named is
// a green build that silently produced something Gatekeeper rejects; this is
// the line that makes that impossible to miss.
console.log(signingBanner(plan));

if (plan.fatal) {
  // A release build that cannot be a release must not produce an artifact at
  // all. Exiting here is deliberate — returning a config would let the build
  // succeed and leave the check to a human reading scrollback.
  console.error("Refusing to build: NH_REQUIRE_SIGNED is set but this "
    + "environment cannot produce a signed, notarized app.");
  process.exit(1);
}

const mac = {
  category: "public.app-category.developer-tools",
  // "The DMG remains the thing a human downloads" (see the header) — but `dmg`
  // was never in this list, so no build has ever produced one. The header
  // described the intent; the list did not implement it. Both are needed and
  // they are not alternatives: dropping `zip` breaks the updater exactly as the
  // header warns, and omitting `dmg` leaves a human with a bare .app in a zip.
  target: ["dir", "zip", "dmg"],
  // `undefined` means "auto-discover from CSC_LINK/CSC_NAME"; `null` means
  // "explicitly do not sign". They are NOT interchangeable — see signing.cjs.
  identity: plan.identity,
  notarize: plan.notarize,
  // hardenedRuntime already defaults to true in electron-builder 26 and is
  // REQUIRED for notarization; stated explicitly so a default change cannot
  // silently make notarization start failing.
  hardenedRuntime: true,
  gatekeeperAssess: false,
};

// The Windows half. Deliberately as close to `mac` as the two OSes allow —
// every difference below is forced by a platform convention, and each one is
// written down in docs/WINDOWS.md.
//
// `target` mirrors the reasoning in this file's header rather than copying its
// list. On macOS the ZIP is load-bearing because Squirrel.Mac updates FROM a
// zip and electron-builder only emits `latest-mac.yml` when a zip target is
// present. On Windows the updater consumes the NSIS installer directly and
// `latest.yml` is emitted for the nsis target — so nsis alone would satisfy the
// updater. `zip` is kept anyway for parity of SHAPE: it is the artifact a human
// can unpack and inspect without running an installer, which is exactly the
// role the .app-in-a-zip plays on the Mac.
//
// NOT signed. There is no Windows code-signing certificate, and unlike macOS
// this does not produce a broken artifact — it produces one SmartScreen warns
// about on first run. That warning is documented honestly in docs/WINDOWS.md
// rather than worked around, and `nhCanAutoUpdate` below stays false: NSIS
// differs from Squirrel.Mac in that it CAN install an unsigned update, but that
// path is unverified on this machine, and shipping an update path nobody has
// watched work is exactly the "guessing wrong" this file's header rejects.
const win = {
  target: ["nsis", "zip"],
  // Same art as desktop/build/icon.icns, so both platforms wear the same
  // mark. Regenerate with packaging/make-win-icon.ps1 (Windows) or any tool
  // that packs the iconset's 16/32/48/64/128/256 PNGs as PNG-compressed ICO
  // members. The source PNGs the icons are cut from are not in this repo.
  icon: "build/icon.ico",
  // The filename carries the verdict, exactly as make-dmg.sh does for the DMG:
  // "An operator cannot upload no_human-0.1.0-UNSIGNED.dmg to a release page
  // believing it is shippable." The same must be true of the .exe, or Windows
  // becomes the soft spot in a rule macOS enforces.
  artifactName: "${productName}-${version}" + plan.artifactTag + ".${ext}",
};

// Ad-hoc re-seal when we are NOT signing for real.
//
// `identity: null` tells electron-builder not to sign AT ALL. That does not
// leave an unsigned bundle: it leaves ELECTRON'S own ad-hoc signature, from the
// prebuilt binary — which electron-builder then INVALIDATES by injecting our
// app and `extraResources` into Contents/. The shipped .app verified as
//     code has no resources but signature indicates they must be present
// and reported `Identifier=Electron`. macOS calls that app DAMAGED, and
// right-click -> Open cannot bypass a broken seal. It is strictly worse than
// shipping something honestly unsigned, and it is what the DMG on disk did.
//
// Re-sealing ad-hoc needs no Apple credential and costs nothing. It turns the
// hard "damaged" failure back into the ordinary "unidentified developer"
// prompt a tester can accept, and it restores the real bundle identifier.
// It does NOT notarize and does NOT enable auto-update — `nhCanAutoUpdate`
// still comes from the signing plan, so this cannot make the updater offer an
// install macOS would refuse.
async function adhocSeal(context) {
  if (context.electronPlatformName !== "darwin") return;
  // `plan.identity !== null`, NOT `mode === "signed"`. An independent reviewer
  // enumerated signingPlan and found THREE modes, not two: `signed-not-notarized`
  // (CSC_LINK or CSC_NAME without the notarization vars) is also a real Developer
  // ID signature -- `identity: undefined` means "auto-discover", not "don't sign".
  // The mode check let the hook run there, and it was harmless only by ordering
  // accident: doSignAfterPack happens after emitAfterPack and --force-resigns.
  // Keyed on identity, this asks the question that actually matters -- "are we
  // signing for real?" -- and stays correct if that ordering ever changes.
  if (plan.identity !== null) return;  // a real signer owns the bundle
  const { execFileSync } = require("child_process");
  const path = require("path");
  const app = path.join(context.appOutDir,
    `${context.packager.appInfo.productFilename}.app`);
  execFileSync("codesign", ["--force", "--deep", "--sign", "-", app],
    { stdio: "inherit" });
  // Verify rather than trust: a silent codesign failure here would ship the
  // exact defect this hook exists to remove.
  execFileSync("codesign", ["--verify", "--deep", "--strict", app],
    { stdio: "inherit" });
  console.log(`  \u2713 ad-hoc sealed and verified: ${app}`);
}

module.exports = {
  appId: "dev.nohuman.board",
  productName: "no_human",
  // buildResources holds icon.icns — the brand mark the dock, Finder and the
  // DMG show. Generated from the site's mark-dark-512.png; without it every
  // build wore Electron's stock atom icon.
  directories: { output: "dist", buildResources: "build" },
  // `nhPackagedFiles`, NOT `build`. electron-builder reads a `build` key out of
  // package.json all by itself whenever it is invoked WITHOUT `--config` —
  // `npx electron-builder --win`, an IDE task, a future CI step. The key here
  // holds only the file list, so that invocation used to produce a green build
  // of a PAYLOAD-LESS installer: no signing plan, no extraResources (so no
  // nh-server), no extraMetadata, none of the reasoning in this file. Under a
  // name electron-builder does not look for, that path cannot silently half-run
  // — it fails outright with nothing to configure. The list stays in
  // package.json because desktop/packagedFiles.test.mjs reads it from there.
  files: require("./package.json").nhPackagedFiles.files,
  asar: true,
  extraResources: [
    { from: "../packaging/dist/nh-server", to: "nh-server" },
    // THE DOCUMENTATION, shipped rather than linked. This was argued the other
    // way first and the argument was wrong, so the reasoning is recorded here.
    //
    // The case for linking only was (a) files in Contents/Resources are
    // reachable only via Show Package Contents, and (b) a bundled copy drifts
    // from the live docs. (a) is true and is why the Help menu opens this file
    // rather than expecting anyone to find it. (b) is BACKWARDS: these files
    // are pinned to the code by `tests/test_readme_claims.py` and ship from the
    // same commit as the binary, so they cannot drift FROM that binary. The
    // artefact that drifts is the SITE, on its own release cadence.
    //
    // And the site is already wrong for exactly this audience: getnohuman.com's
    // docs page contains no occurrence of "dmg", ".app", "Applications folder"
    // or "installer", and its Before-you-start section requires Python, uv, git
    // and a checkout. The Help menu exists only in the packaged app, so linking
    // alone sent its only readers to instructions for a different install.
    //
    // Cost measured: 7 KB of quickstart against a ~148 MB DMG. It also gives
    // the app an OFFLINE documentation route, which a URL cannot.
    { from: "../docs/quickstart.md", to: "docs/quickstart.md" },
    { from: "../docs/configuration.md", to: "docs/configuration.md" },
    // MIT asks for the licence text to travel with substantial copies; the DMG
    // carries only the .app, so the text rides inside Contents/Resources.
    { from: "../LICENSE", to: "LICENSE" },
    // The built bundle carried our MIT text and no notice for Electron or
    // Chromium at all (verified on desktop/dist/mac-arm64/no_human.app).
    //
    // NOT because ours overwrote theirs — that was the first explanation given
    // here and it was wrong. Electron's LICENSE and LICENSES.chromium.html live
    // at the TOP of node_modules/electron/dist/, beside Electron.app; nothing
    // named LICENSE has ever existed in Contents/Resources, so the line above
    // collided with nothing. electron-builder DELETES them:
    //   * electronMac.js:219-220 unlinkIfExists(appOutDir/LICENSE) and
    //     (appOutDir/LICENSES.chromium.html) — appOutDir is dist/mac-arm64/,
    //     one level ABOVE the .app;
    //   * ElectronFramework.js:236-239 renames LICENSE -> LICENSE.electron.txt
    //     only when NOT macOS (`isMac ? Promise.resolve() : rename(...)`), so
    //     the step that would have preserved it never runs here.
    // Chromium's BSD terms require the notice to be reproduced with a binary
    // distribution, so both ride back in explicitly, at names that cannot
    // collide with ours. Guarded by packagedFiles.test.mjs.
    //
    // LICENSES.chromium.html is ~14 MB on a ~140 MB DMG. That is the price of
    // redistributing Chromium, not an oversight to trim later.
    { from: "node_modules/electron/dist/LICENSE", to: "LICENSE.electron.txt" },
    { from: "node_modules/electron/dist/LICENSES.chromium.html",
      to: "LICENSES.chromium.html" },
  ],
  // Read at runtime by main.mjs (packagedSigning) and never from the live
  // environment, so a user cannot enable the update path by exporting a var.
  extraMetadata: {
    nhSigning: plan.mode,
    nhCanAutoUpdate: plan.canAutoUpdate,
  },
  mac,
  win,
  // Darwin-only by construction: adhocSeal returns immediately unless
  // electronPlatformName is "darwin". Windows needs NO afterPack equivalent —
  // the macOS hook exists to repair a signature electron-builder INVALIDATES by
  // injecting into Contents/, and Windows has no equivalent seal to break. An
  // unsigned .exe here is simply unsigned, not "damaged".
  afterPack: adhocSeal,
  // Generates latest-mac.yml locally. `--publish never` on every script means
  // nothing is ever uploaded; this block only tells the updater where to LOOK
  // once a release exists.
  publish: [{ provider: "github", owner: "no-human-ai", repo: "no_human" }],
};
