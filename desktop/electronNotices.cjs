// WHY A beforePack GUARD EXISTS FOR TWO FILES.
// Electron 42 removed the package's `postinstall` hook: the binary and its
// `dist/` are fetched on FIRST EXECUTION of the electron binary, not at install
// time. `npm ci` therefore leaves `node_modules/electron/dist` absent — and
// app-builder-lib's file matcher FAILS OPEN on a missing `extraResources`
// source (fileMatcher.js: `log.warn("file source doesn't exist"); return;`).
// The result, reproduced from a clean `npm ci`: a green build whose app carries
// NEITHER Electron's MIT notice NOR Chromium's BSD notice. Shipping that is a
// redistribution violation, and the packaged bundle looks fine.
//
// So the build stops here instead. This runs before any packing work, names
// the file that is missing, and says how to materialise it (running the
// electron binary once triggers the download).
//
// WHY THIS IS ITS OWN MODULE and not a second export off the config.
// electron-builder validates the exported config object against a JSON schema
// with `additionalProperties: false`. Hanging a helper off `module.exports` of
// electron-builder.config.cjs makes EVERY `npm run dist*` die at
// `Packager.validateConfig` with "configuration has an unknown property" —
// before beforePack is ever reached, on every platform, notices present or not.
// The test needs a handle on this function; the config must export nothing but
// the config. Hence a module both of them require.

const fs = require("fs");
const path = require("path");

const LICENCE_SOURCES = [
  "node_modules/electron/dist/LICENSE",
  "node_modules/electron/dist/LICENSES.chromium.html",
];

function assertElectronNoticesPresent(root = __dirname) {
  const missing = LICENCE_SOURCES.filter(
    (rel) => !fs.existsSync(path.join(root, rel)));
  if (missing.length === 0) return;
  throw new Error(
    "refusing to package without Electron's and Chromium's licence notices.\n" +
    "  missing: " + missing.join(", ") + "\n\n" +
    "  Electron 42+ has no postinstall hook, so `npm ci` alone does not\n" +
    "  materialise node_modules/electron/dist. Run the binary once to fetch it:\n" +
    "      node -e \"require('child_process').execFileSync(require('electron'), ['--version'], {stdio:'ignore'})\"\n" +
    "  (`npm run ensure-electron` does exactly that.)\n\n" +
    "  This is a hard stop because app-builder-lib only WARNS about a missing\n" +
    "  extraResources source, and the app would ship without a notice whose\n" +
    "  redistribution terms require it.");
}

module.exports = { LICENCE_SOURCES, assertElectronNoticesPresent };
