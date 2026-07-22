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
                                    onReenterToken }) {
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
      isMac ? { role: "close" } : { role: "quit" },
    ],
  });
  template.push({ role: "editMenu" });
  template.push({
    label: "View",
    submenu: [
      nav("Board", "board", "CmdOrCtrl+1"),
      nav("Stats", "stats", "CmdOrCtrl+2"),
      nav("Settings", "settings", "CmdOrCtrl+3"),
      { type: "separator" },
      { role: "reload" },
      { role: "resetZoom" },
      { role: "zoomIn" },
      { role: "zoomOut" },
      ...(isDev ? [{ type: "separator" }, { role: "toggleDevTools" }] : []),
    ],
  });
  template.push({ role: "windowMenu" });
  return template;
}
