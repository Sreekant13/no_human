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
  target: ["dir", "zip"],
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

module.exports = {
  appId: "dev.nohuman.board",
  productName: "no_human",
  // buildResources holds icon.icns — the brand mark the dock, Finder and the
  // DMG show. Generated from the site's mark-dark-512.png; without it every
  // build wore Electron's stock atom icon.
  directories: { output: "dist", buildResources: "build" },
  files: require("./package.json").build.files,
  asar: true,
  extraResources: [
    { from: "../packaging/dist/nh-server", to: "nh-server" },
    // MIT asks for the licence text to travel with substantial copies; the DMG
    // carries only the .app, so the text rides inside Contents/Resources.
    { from: "../LICENSE", to: "LICENSE" },
  ],
  // Read at runtime by main.mjs (packagedSigning) and never from the live
  // environment, so a user cannot enable the update path by exporting a var.
  extraMetadata: {
    nhSigning: plan.mode,
    nhCanAutoUpdate: plan.canAutoUpdate,
  },
  mac,
  // Generates latest-mac.yml locally. `--publish never` on every script means
  // nothing is ever uploaded; this block only tells the updater where to LOOK
  // once a release exists.
  publish: [{ provider: "github", owner: "eyalgolan", repo: "no_human" }],
};
