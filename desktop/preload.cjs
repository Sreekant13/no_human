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

// `npm_package_version` is set by `npm run`, and NOTHING sets it in a packaged
// app — this read was always the literal string "dev" in every shipped DMG.
// That is load-bearing now: the update UI compares this against the released
// version, and "dev" compares as "not newer" than everything, so a stale
// install would never be told it was stale. electron-builder writes the real
// version into the packaged package.json, so read that and keep the env var
// only as the `npm run desktop` fallback.
let appVersion = process.env.npm_package_version || "dev";
try {
  appVersion = require("./package.json").version || appVersion;
} catch {
  /* keep the fallback — a missing package.json is already guarded elsewhere */
}

contextBridge.exposeInMainWorld("nhDesktop", {
  shell: true,
  version: appVersion,
  // The application menu (main process) drives the board's own navigation:
  // main sends "nh:menu" with a page id ("board"/"stats"/"settings") or
  // "new-task"; App.jsx subscribes and updates its existing state. Returns an
  // unsubscribe so React effects can clean up.
  onMenu: (callback) => {
    const listener = (_event, action) => callback(action);
    ipcRenderer.on("nh:menu", listener);
    return () => ipcRenderer.removeListener("nh:menu", listener);
  },
  // Updates. The shell finds them; the board decides what to say and when the
  // user acts. `download` and `install` are separate on purpose — the operator
  // asked that users be informed and then choose, so nothing moves bytes or
  // restarts the app without a distinct click.
  onUpdate: (callback) => {
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on("nh:update", listener);
    return () => ipcRenderer.removeListener("nh:update", listener);
  },
  checkForUpdates: () => ipcRenderer.invoke("nh:update-check"),
  downloadUpdate: () => ipcRenderer.invoke("nh:update-download"),
  installUpdate: () => ipcRenderer.invoke("nh:update-install"),
  deferUpdate: (version) => ipcRenderer.invoke("nh:update-defer", version),
});
