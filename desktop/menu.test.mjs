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

test("buildMenuTemplate: View has Board/Stats/Settings nav with Cmd 1/2/3", () => {
  const nav = [];
  const t = buildMenuTemplate({ isMac: true, isDev: false, onNavigate: (p) => nav.push(p), onNewTask: () => {} });
  const view = submenu(t, "View");
  const board = view.find((i) => i.label === "Board");
  const stats = view.find((i) => i.label === "Stats");
  const settings = view.find((i) => i.label === "Settings");
  assert.ok(board && stats && settings, "View has all three nav items");
  assert.equal(board.accelerator, "CmdOrCtrl+1");
  assert.equal(stats.accelerator, "CmdOrCtrl+2");
  assert.equal(settings.accelerator, "CmdOrCtrl+3");
  board.click(); settings.click();
  assert.deepEqual(nav, ["board", "settings"], "clicks route to the renderer with the page id");
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
