// Minimal preload: contextIsolation is ON and nodeIntegration OFF, so the
// web app runs exactly as it does in a browser. The only bridge is a marker
// letting the UI know it's inside the desktop shell (used by e2e smoke and,
// later, notification dedupe in E3).
const { contextBridge, ipcRenderer } = require("electron");

// First-run credential screen (token.html) only. Deliberately separate from
// nhDesktop: the board never needs these, and the credential never crosses
// back — saveToken returns {ok} or {error}, never the value. `mode` selects
// the billing path: "subscription" (default) or "api_key" (BYO Anthropic key).
contextBridge.exposeInMainWorld("nhSetup", {
  saveToken: (value, mode) => ipcRenderer.invoke("nh:save-token", value, mode),
  dismiss: () => ipcRenderer.invoke("nh:dismiss"),
  quit: () => ipcRenderer.invoke("nh:quit"),
});

contextBridge.exposeInMainWorld("nhDesktop", {
  shell: true,
  version: process.env.npm_package_version || "dev",
  // The application menu (main process) drives the board's own navigation:
  // main sends "nh:menu" with a page id ("board"/"stats"/"settings") or
  // "new-task"; App.jsx subscribes and updates its existing state. Returns an
  // unsubscribe so React effects can clean up.
  onMenu: (callback) => {
    const listener = (_event, action) => callback(action);
    ipcRenderer.on("nh:menu", listener);
    return () => ipcRenderer.removeListener("nh:menu", listener);
  },
});
