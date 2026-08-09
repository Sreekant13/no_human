import test from "node:test";
import assert from "node:assert/strict";
import { buildMenuTemplate } from "./menu.mjs";

// Flatten a menu template's leaf items for assertions.
function items(template) {
  const out = [];
  for (const top of template) {
    if (Array.isArray(top.submenu)) for (const it of top.submenu) out.push(it);
  }
  return out;
}
function submenu(template, label) {
  return (template.find((t) => t.label === label) || {}).submenu || [];
}

test("buildMenuTemplate: View has Board/Backlog/Stats/Settings nav with Cmd 1/2/3/4", () => {
  const nav = [];
  const t = buildMenuTemplate({ isMac: true, isDev: false, onNavigate: (p) => nav.push(p), onNewTask: () => {} });
  const view = submenu(t, "View");
  const board = view.find((i) => i.label === "In progress");
  const backlog = view.find((i) => i.label === "Backlog");
  const stats = view.find((i) => i.label === "Stats");
  const settings = view.find((i) => i.label === "Settings");
  assert.ok(board && backlog && stats && settings, "View has all four nav items");
  assert.equal(board.accelerator, "CmdOrCtrl+1");
  assert.equal(backlog.accelerator, "CmdOrCtrl+2");
  assert.equal(stats.accelerator, "CmdOrCtrl+3");
  assert.equal(settings.accelerator, "CmdOrCtrl+4");
  board.click(); backlog.click(); settings.click();
  assert.deepEqual(nav, ["board", "backlog", "settings"], "clicks route to the renderer with the page id");
});

// The desktop menu is only a real route if the renderer acts on the page id it
// posts. App.jsx's "nh:menu" handler enumerates the pages it accepts, so a menu
// entry naming a page the handler drops is a dead menu item.
test("the renderer's menu handler accepts the backlog page id the View menu sends", async () => {
  const { readFileSync } = await import("node:fs");
  const { fileURLToPath } = await import("node:url");
  const appJsx = readFileSync(
    fileURLToPath(new URL("../web/src/App.jsx", import.meta.url)), "utf8");
  const handler = appJsx.match(/window\.nhDesktop\?\.onMenu[\s\S]*?\}\);/)?.[0];
  assert.ok(handler, "App.jsx must wire window.nhDesktop.onMenu");
  assert.match(handler, /action === ["']backlog["']/,
    "the nh:menu handler must route the backlog page id, not silently drop it");
});

test("buildMenuTemplate: New Task uses CmdOrCtrl+N — never bare 'n' (renderer owns that)", () => {
  let opened = 0;
  const t = buildMenuTemplate({ isMac: true, isDev: false, onNavigate: () => {}, onNewTask: () => { opened++; } });
  const nt = items(t).find((i) => i.label === "New Task");
  assert.ok(nt, "New Task item exists");
  assert.equal(nt.accelerator, "CmdOrCtrl+N");
  nt.click();
  assert.equal(opened, 1);
});

test("buildMenuTemplate: dev-only devtools, and an Edit menu for copy/paste", () => {
  const dev = buildMenuTemplate({ isMac: true, isDev: true, onNavigate: () => {}, onNewTask: () => {} });
  const prod = buildMenuTemplate({ isMac: true, isDev: false, onNavigate: () => {}, onNewTask: () => {} });
  const hasDevtools = (t) => items(t).some((i) => i.role === "toggleDevTools");
  assert.equal(hasDevtools(dev), true, "devtools present in dev builds");
  assert.equal(hasDevtools(prod), false, "devtools hidden in packaged builds");
  // A custom application menu REPLACES the default, so copy/paste in inputs
  // dies unless an Edit menu (role) is included.
  assert.ok(prod.some((t) => t.role === "editMenu"), "Edit menu present");
});

test("buildMenuTemplate: macOS gets the app menu; other platforms do not", () => {
  const mac = buildMenuTemplate({ isMac: true, isDev: false, onNavigate: () => {}, onNewTask: () => {} });
  const win = buildMenuTemplate({ isMac: false, isDev: false, onNavigate: () => {}, onNewTask: () => {} });
  assert.equal(mac[0].role, "appMenu", "macOS app menu first");
  assert.ok(!win.some((t) => t.role === "appMenu"), "no app menu off macOS");
});

test("off macOS the File menu still carries every recovery path, ending in quit", () => {
  // There is no appMenu off macOS, so File is the ONLY home for these. "New
  // Task" is the way back to work, "Re-enter Claude Token…" the only remedy
  // for a revoked token, "Check for Updates…" the only way back to a skipped
  // update — losing any of them to a mac-only branch would strand the Windows
  // app with no in-app recovery. Verified on real Windows 2026-08-05.
  const t = buildMenuTemplate({
    isMac: false, isDev: false, onNavigate() {}, onNewTask() {},
    onReenterToken() {}, onRerunSetup() {}, onCheckForUpdates() {},
  });
  const file = t.find((m) => m.label === "File");
  for (const label of ["New Task", "Re-enter Claude Token…", "Re-run Setup…",
                       "Check for Updates…"]) {
    assert.ok(file.submenu.some((i) => i.label === label), `${label} reachable off macOS`);
  }
  const last = file.submenu[file.submenu.length - 1];
  assert.equal(last.role, "quit", "File ends in quit off macOS (close would strand a tray app)");
});

test("File menu exposes Re-enter Claude Token and it routes", () => {
  let called = 0;
  const t = buildMenuTemplate({
    isMac: true, isDev: false, onNavigate() {}, onNewTask() {},
    onReenterToken: () => { called += 1; },
  });
  const file = t.find((m) => m.label === "File");
  const item = file.submenu.find((i) => i.label === "Re-enter Claude Token…");
  assert.ok(item, "the only in-app remedy for a bad token must be present");
  item.click();
  assert.equal(called, 1, "it must invoke onReenterToken");
});

test("the token item is omitted when no handler is supplied", () => {
  const t = buildMenuTemplate({ isMac: true, isDev: false, onNavigate() {}, onNewTask() {} });
  const file = t.find((m) => m.label === "File");
  assert.equal(file.submenu.some((i) => i.label === "Re-enter Claude Token…"), false);
});

// The wizard writes onboarding.completed once and NOTHING wrote it back, so a
// user who set themselves up wrong — no proven repo, no projects, history never
// scanned, none of the nine steps gates — could not reach the screen that fixes
// it. The only route was hand-editing config.yaml and restarting the server.
test("File menu exposes Re-run Setup and it routes", () => {
  let called = 0;
  const t = buildMenuTemplate({
    isMac: true, isDev: false, onNavigate() {}, onNewTask() {},
    onRerunSetup: () => { called += 1; },
  });
  const file = t.find((m) => m.label === "File");
  const item = file.submenu.find((i) => i.label === "Re-run Setup…");
  assert.ok(item, "the only in-app route back into onboarding must be present");
  item.click();
  assert.equal(called, 1, "it must invoke onRerunSetup");
});

test("the setup item is omitted when no handler is supplied", () => {
  const t = buildMenuTemplate({ isMac: true, isDev: false, onNavigate() {}, onNewTask() {} });
  const file = t.find((m) => m.label === "File");
  assert.equal(file.submenu.some((i) => i.label === "Re-run Setup…"), false);
});

// The menu item is only a route if main.mjs actually resets AND reloads: the
// board reads onboarding status once, at load, so a reset with no reload leaves
// the user looking at the same board and concluding the item is broken.
test("main.mjs wires Re-run Setup to the reset endpoint and reloads the board", async () => {
  const { readFileSync } = await import("node:fs");
  const { fileURLToPath } = await import("node:url");
  const src = readFileSync(fileURLToPath(new URL("./main.mjs", import.meta.url)), "utf8");
  assert.match(src, /onRerunSetup\s*:/, "buildAppMenu no longer passes onRerunSetup");
  const handler = src.match(/onRerunSetup:[\s\S]*?\n {4}\},/)?.[0];
  assert.ok(handler, "could not locate the onRerunSetup handler");
  assert.match(handler, /\/api\/onboarding\/reset/,
    "it must call the reset endpoint, not just open a window");
  assert.match(handler, /method: "POST"/);
  assert.match(handler, /loadBoardOrError\(win\)/,
    "without the reload the flag changes and the screen does not");
});

test("File menu exposes Check for Updates and it routes", () => {
  // Once a user picks "Later", the automatic prompt for that version is gone
  // for good. This item is the only way back to it, so its absence turns
  // "later" into "never" with no error and nothing to notice.
  let called = 0;
  const t = buildMenuTemplate({
    isMac: true, isDev: false, onNavigate() {}, onNewTask() {},
    onCheckForUpdates: () => { called += 1; },
  });
  const file = t.find((m) => m.label === "File");
  const item = file.submenu.find((i) => i.label === "Check for Updates…");
  assert.ok(item, "the manual update path must be reachable from the menu");
  item.click();
  assert.equal(called, 1, "it must invoke onCheckForUpdates");
});

test("the update item is omitted when no handler is supplied", () => {
  const t = buildMenuTemplate({ isMac: true, isDev: false, onNavigate() {}, onNewTask() {} });
  const file = t.find((m) => m.label === "File");
  assert.equal(file.submenu.some((i) => i.label === "Check for Updates…"), false);
});

// The bundle previously carried NO user documentation and the app had no route
// to any. Both halves are now addressed: the docs ship as extraResources, and
// Help opens the bundled copy first.
//
// CORRECTION, recorded because it was asserted as fact in four places: "zero
// .md files in the bundle" is FALSE as written. `app.asar` carries two
// (`ms/license.md`, `sax/LICENSE.md`). Both are licences, so "no user
// documentation" stood — but `find` over the mounted volume CANNOT see inside
// an asar, so the instrument that produced that claim could never have found
// its own counterexample.
test("buildMenuTemplate: Help offers the BUNDLED quickstart first, then the site", () => {
  const opened = [];
  const t = buildMenuTemplate({
    isMac: true, isDev: false, onNavigate: () => {}, onNewTask: () => {},
    onOpenDocs: (which) => { opened.push(which); },
  });
  const help = t.find((m) => m.role === "help");
  assert.ok(help, "a Help menu exists");
  const items = (help.submenu || []).filter((i) => i.label);
  assert.equal(items[0].label, "no_human Quickstart",
    "the OFFLINE copy must come first — the site's docs assume a git checkout " +
    "and never mention a .dmg, so they describe a different install");
  assert.equal(items[1].label, "Documentation (Online)");
  items[0].click(); items[1].click();
  assert.deepEqual(opened, ["quickstart", "site"],
    "each item must ask for its own target, not share one handler");
});

test("buildMenuTemplate: Help gains an 'About no_human' item that fires onShowAbout", () => {
  let shown = 0;
  const t = buildMenuTemplate({
    isMac: true, isDev: false, onNavigate: () => {}, onNewTask: () => {},
    onOpenDocs: () => {}, onShowAbout: () => { shown++; },
  });
  const help = t.find((m) => m.role === "help");
  const about = (help.submenu || []).find((i) => i.label === "About no_human");
  assert.ok(about, "an About item exists when onShowAbout is supplied");
  about.click();
  assert.equal(shown, 1, "clicking About invokes onShowAbout");
});

test("buildMenuTemplate: no About item when onShowAbout is absent", () => {
  const t = buildMenuTemplate({
    isMac: true, isDev: false, onNavigate: () => {}, onNewTask: () => {},
    onOpenDocs: () => {},
  });
  const help = t.find((m) => m.role === "help");
  const about = (help.submenu || []).find((i) => i.label === "About no_human");
  assert.equal(about, undefined, "no About item without a handler (no dead menu entry)");
});

test("buildMenuTemplate: no Help menu when no docs handler is supplied", () => {
  const t = buildMenuTemplate({
    isMac: true, isDev: false, onNavigate: () => {}, onNewTask: () => {},
  });
  assert.equal(t.find((m) => m.role === "help"), undefined,
    "an unwired build gets no empty Help menu");
});
