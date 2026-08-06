// Minimal electron stand-in. Records the handlers main.mjs registers so a test
// can invoke them and observe real behaviour (preventDefault, quit, IPC).
import os from "node:os";
import path from "node:path";
export const calls = { quit: 0, exit: 0, handlers: new Map(), ipc: new Map(),
                       opened: [], nav: new Map(), badge: [], sent: [] };
export function reset() {
  calls.quit = 0; calls.exit = 0; calls.handlers.clear(); calls.ipc.clear();
  calls.opened.length = 0; calls.nav.clear(); calls.badge.length = 0;
  calls.sent.length = 0;
}
let readyResolve;
export const readyGate = new Promise((r) => { readyResolve = r; });
export function fireReady() { readyResolve(); }

export const app = {
  isPackaged: false,
  requestSingleInstanceLock: () => true,
  whenReady: () => readyGate,
  on: (event, fn) => { calls.handlers.set(event, fn); },
  quit: () => { calls.quit += 1; },
  exit: () => { calls.exit += 1; },
  getAppPath: () => process.cwd(),
  setBadgeCount: (n) => { calls.badge.push(n); },
  // The updater needs both: a version to compare against the feed, and a
  // directory to persist "Later" into.
  getVersion: () => "0.1.0",
  getPath: (name) => path.join(os.tmpdir(), `nh-stub-${name}`),
};
export const ipcMain = { handle: (ch, fn) => { calls.ipc.set(ch, fn); } };
// RECORDS what is handed to the OS: a no-op double made the scheme guard
// untestable, so deleting it survived every test.
export const shell = { openExternal: (u) => { calls.opened.push(u); } };
export const nativeTheme = { shouldUseDarkColors: false };
export const nativeImage = { createFromBuffer: () => ({ setTemplateImage() {} }) };
export const Menu = { buildFromTemplate: (t) => t, setApplicationMenu: () => {} };
export function Tray() {
  return { setToolTip() {}, setContextMenu() {}, on() {} };
}
export function BrowserWindow(opts) {
  const win = {
    loaded: [],
    loadGen: 0,
    webContents: {
      on(evt, fn) { calls.nav.set(evt, fn); },
      // RECORDS: a no-op send made every main→renderer message untestable, so
      // deleting the update notification would have kept the suite green.
      send(channel, payload) { calls.sent.push({ channel, payload }); },
      setWindowOpenHandler(fn) { calls.nav.set("window-open", fn); },
    },
    async loadURL(u) {
      // A test can hold this open so a LATER navigation supersedes it while it
      // is still in flight — the only way to exercise the supersede guards.
      if (BrowserWindow.slowLoadUrlMs) {
        const mine = ++win.loadGen;
        await new Promise((r) => setTimeout(r, BrowserWindow.slowLoadUrlMs));
        // Electron aborts an in-flight load when a newer one starts; the old
        // promise rejects with ERR_ABORTED. A double that resolves both would
        // report a collision that cannot happen — and hide one that can.
        if (mine !== win.loadGen) throw new Error(`ERR_ABORTED (-3) loading '${u}'`);
      }
      // Electron rejects loadURL when the server dies between probe and load.
      if (BrowserWindow.failLoadURL) {
        win.loaded.push(`url-failed:${u}`);
        throw new Error("ERR_CONNECTION_REFUSED (-102) loading '" + u + "'");
      }
      win.loaded.push(`url:${u}`);
    },
    async loadFile(f, opts) {
      win.loadGen += 1;                     // supersedes any in-flight loadURL
      // May be `true` (fail every file) or a filename substring, so a test can
      // fail token.html while letting the error page through — otherwise the
      // recovery is indistinguishable from the failure.
      const want = BrowserWindow.failLoadFile;
      if (want === true || (typeof want === "string" && f.includes(want))) {
        win.loaded.push(`file-failed:${f.split("/").pop()}`);
        throw new Error("ERR_FILE_NOT_FOUND (-6) loading '" + f + "'");
      }
      const q = opts?.query ?? {};
      const tag = q.reason ? `?${q.reason}` : (q.canReturn ? "?canReturn" : "");
      win.loaded.push(`file:${f.split("/").pop()}${tag}`);
    },
    // once() RECORDS: with a no-op double, deleting the ready-to-show reveal
    // was invisible — and that ships an app whose window never appears.
    once(evt, fn) { win.onceHandlers[evt] = fn; },
    onceHandlers: {},
    shown: 0,
    on() {}, show() { win.shown += 1; }, focus() {}, hide() {},
    // RECORDS: a no-op double would let the live theme re-colour be deleted
    // with the suite green, and the win32 title-bar controls would silently
    // keep the previous theme's colours until the app restarted.
    setTitleBarOverlay(o) { win.overlay = o; },
    isDestroyed: () => false, isMinimized: () => false, restore() {},
  };
  win.opts = opts || {};
  BrowserWindow.last = win;
  return win;
}
