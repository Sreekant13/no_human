// Pure application-menu template builder — no electron imports, so the menu's
// structure (view navigation, accelerators, dev-only devtools) is unit-testable
// with `node --test`. main.mjs feeds it callbacks and hands the result to
// Menu.buildFromTemplate / Menu.setApplicationMenu.
//
// Accelerators deliberately avoid bare keys: the renderer owns a global "n"
// for New Task (web/src/keyboardShortcut.js), so the menu uses CmdOrCtrl+N and
// never shadows it. A custom application menu REPLACES the platform default, so
// the Edit menu (copy/paste/select-all) must be included explicitly or text
// inputs lose it.
export function buildMenuTemplate({ isMac, isDev, onNavigate, onNewTask,
                                    onReenterToken, onRerunSetup, onCheckForUpdates,
                                    onOpenDocs, onShowAbout }) {
  const nav = (label, page, accelerator) => ({
    label, accelerator, click: () => onNavigate(page),
  });
  const template = [];
  if (isMac) template.push({ role: "appMenu" });
  template.push({
    label: "File",
    submenu: [
      { label: "New Task", accelerator: "CmdOrCtrl+N", click: () => onNewTask() },
      { type: "separator" },
      // The only way back to the setup screen once a token is on disk. A typo'd
      // or revoked token otherwise strands the app: hasToken() stays true, so
      // first-run never re-triggers, and the board either fails to open or
      // opens and fails every task with no in-app remedy.
      ...(onReenterToken
        ? [{ label: "Re-enter Claude Token…", click: () => onReenterToken() },
           { type: "separator" }]
        : []),
      // The same class of problem as the item above, applied to setup rather
      // than the token: the wizard writes `onboarding.completed` once and there
      // was nothing anywhere that wrote it back, so a user who realises on day
      // two that they set it up wrong (no proven repo, no projects, history
      // never scanned — none of the eight steps gates) could not reach the screen
      // that fixes it. Resetting is not a wipe; it only makes the wizard render
      // again over the state that is already there.
      ...(onRerunSetup
        ? [{ label: "Re-run Setup…", click: () => onRerunSetup() },
           { type: "separator" }]
        : []),
      // The manual half of "download it then or when they want": a user who
      // chose Later has silenced the automatic prompt for that version, so this
      // is the ONLY way back to it. Without it, "Later" is indistinguishable
      // from "never".
      ...(onCheckForUpdates
        ? [{ label: "Check for Updates…", click: () => onCheckForUpdates() },
           { type: "separator" }]
        : []),
      isMac ? { role: "close" } : { role: "quit" },
    ],
  });
  template.push({ role: "editMenu" });
  template.push({
    label: "View",
    submenu: [
      nav("In progress", "board", "CmdOrCtrl+1"),
      // Backlog sits directly under In progress here for the same reason it
      // sits under it in the sidebar's Work group: it is the list work is
      // picked up FROM. Stats/Settings shift down a number accordingly — the
      // accelerators follow menu order, they are not stable identifiers.
      nav("Backlog", "backlog", "CmdOrCtrl+2"),
      nav("Stats", "stats", "CmdOrCtrl+3"),
      nav("Settings", "settings", "CmdOrCtrl+4"),
      { type: "separator" },
      { role: "reload" },
      { role: "resetZoom" },
      { role: "zoomIn" },
      { role: "zoomOut" },
      ...(isDev ? [{ type: "separator" }, { role: "toggleDevTools" }] : []),
    ],
  });
  template.push({ role: "windowMenu" });
  // WITHOUT THIS MENU THE INSTALLED APP HAS NO ROUTE TO DOCUMENTATION AT ALL.
  // That was established by mounting the round-3 DMG: the only documents under
  // Contents/Resources were LICENSE, LICENSE.electron.txt and
  // LICENSES.chromium.html — redistribution notices, not something a user reads
  // to learn the product.
  //
  // THE ORIGINAL ARGUMENT HERE WAS LINK-ONLY, AND IT WAS REVERSED. It ran:
  // shipping a README inside the bundle answers nobody, because it lands in
  // Contents/Resources, which a user reaches only via right-click → Show
  // Package Contents; and a copy of the docs that ships on a build cadence
  // drifts from the docs that do not. It is kept here because both halves were
  // wrong in instructive ways, and the reversal is the current design: the
  // bundle now ships `docs/quickstart.md` and `docs/configuration.md` as
  // extraResources, and this Help menu opens the BUNDLED copy first and the
  // live site second.
  //
  // Why the argument failed. The reachability half is real but is an argument
  // for THIS MENU ITEM, not against shipping the file — the app opens it, so
  // nobody has to find it. The drift half is backwards: the shipped copy leaves
  // on the same commit as the binary and is pinned to the code by
  // tests/test_readme_claims.py, while the SITE moves on its own cadence and
  // already describes a git checkout with Python and uv, which is not the
  // install a packaged-app user has. And a link is worth nothing to a first-run
  // user with no network, which is exactly when a quickstart is read.
  //
  // The premise underneath it was also false as asserted: "zero .md files in
  // the bundle" was measured with `find` over the mounted volume, which cannot
  // see inside an asar — app.asar carried two all along (both licences, so "no
  // user documentation" survived, but the instrument could never have found its
  // own counterexample). menu.test.mjs records this as a CORRECTION block.
  //
  // `role: "help"` rather than a plain label: macOS gives the Help role its
  // conventional position and its searchable Help field, and a custom
  // application menu replaces the platform default, so nothing supplies it
  // otherwise.
  //
  // Conditional, like Re-enter Token and Check for Updates above: a caller that
  // passes no handler gets a byte-identical menu to before, so this cannot
  // perturb the existing wiring or its tests.
  if (onOpenDocs) {
    template.push({
      role: "help",
      submenu: [
        // The BUNDLED quickstart first, and it is the default for two reasons:
        // it works offline, and it describes THIS install. The live site's docs
        // page assumes a git checkout with Python, uv and the CLI — it never
        // mentions a .dmg or the Applications folder — so for a packaged-app
        // user it is documentation for a different product.
        { label: "no_human Quickstart", click: () => onOpenDocs("quickstart") },
        { type: "separator" },
        // Secondary, and honestly labelled as the online one: it is the copy
        // that gains new material between releases, and the copy that can be
        // wrong about the version actually installed.
        { label: "Documentation (Online)", click: () => onOpenDocs("site") },
        // "About no_human" in Help too, not only the macOS app menu: Windows and
        // Linux have no appMenu, so this is the ONLY About entry there. On macOS
        // it duplicates the app-menu item, which is harmless and expected.
        ...(onShowAbout
          ? [{ type: "separator" }, { label: "About no_human", click: () => onShowAbout() }]
          : []),
      ],
    });
  }
  return template;
}
