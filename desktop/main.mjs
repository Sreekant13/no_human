// no_human desktop shell — Phase E1 (attach-only).
//
// Points a native window at the LOCAL nh server (same-origin, so the web
// app's relative fetch / location.host WebSocket / SSE work untouched).
// External links (PR/CI URLs) open in the OS browser, never in-window.
// If the server isn't reachable, a friendly error page renders with retry —
// a blank window is the one unacceptable failure mode.

import { app, BrowserWindow, Menu, nativeImage, nativeTheme, shell, Tray } from "electron";
import { parseBadgeCount } from "./badge.mjs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  DEFAULT_ORIGIN,
  ensureServer,
  isAppOrigin,
  probe,
  stopServer,
} from "./server.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ORIGIN = process.env.NH_ORIGIN || DEFAULT_ORIGIN;

let win = null;
// E2: retain the ensure result — its child is the ONLY legitimate kill
// target on quit. An attached (operator-started) server is never stopped.
let serverState = null;
let tray = null;
let quitting = false;

// 16x16 template circle, GENERATED + verified by scripts (review finding:
// the previous hand-typed base64 was corrupt — invisible tray, dead feature).
// desktop/trayIcon.test.mjs decodes + inflates it so corruption can't return.
export const TRAY_ICON_B64 =
  "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAARUlEQVR42mNgoCHoQcNkafwP" +
  "xUQbhK4RHeM1hJBmgoYQoxnZELJsx+kKUjRjdQVVDKDICxQHIlWikeKERJWkTJXMRBIAAFkt" +
  "fBGd64lBAAAAAElFTkSuQmCC";

function trayIcon() {
  const img = nativeImage.createFromBuffer(Buffer.from(TRAY_ICON_B64, "base64"));
  img.setTemplateImage(true);
  return img;
}

function serverLabel() {
  if (!serverState) return "server: probing…";
  return {
    attached: "server: attached (operator-owned)",
    spawned: "server: spawned by the app",
    failed: `server: unreachable (${serverState.reason})`,
  }[serverState.status] ?? "server: unknown";
}

function buildTray() {
  tray = new Tray(trayIcon());
  tray.setToolTip("no_human");
  const rebuild = () => tray.setContextMenu(Menu.buildFromTemplate([
    { label: "Open no_human", click: showWindow },
    { label: serverLabel(), enabled: false },
    { type: "separator" },
    { label: "Quit no_human", click: () => { quitting = true; app.quit(); } },
  ]));
  rebuild();
  tray.on("click", showWindow);
  // Keep the server line fresh when the menu is about to show (macOS builds
  // the menu lazily, so rebuilding on click keeps it truthful).
  tray.on("mouse-enter", rebuild);
}

function showWindow() {
  if (win && !win.isDestroyed()) {
    win.show();
    win.focus();
  } else {
    createWindow();
  }
}

// Defense-in-depth (review finding): only ever hand web schemes to the OS —
// the board sanitizes its links today, but the shell must not TRUST that.
function openExternallyIfWeb(url) {
  if (!/^https?:\/\//i.test(url)) return;
  if (process.env.NH_TEST_LOG) {
    // Test mode observes routing without side effects (the smoke must not
    // actually open browser tabs on the operator's machine).
    import("node:fs").then((fs) =>
      fs.appendFileSync(process.env.NH_TEST_LOG, `openExternal ${url}\n`));
    return;
  }
  shell.openExternal(url);
}

function routeExternally(contents) {
  // target="_blank" (Board/SlideOver PR links) → OS browser, no child window.
  contents.setWindowOpenHandler(({ url }) => {
    if (!isAppOrigin(url, ORIGIN)) openExternallyIfWeb(url);
    return { action: "deny" };
  });
  // ONE will-navigate handler owns everything (review finding: a second
  // global handler double-fired on nh://retry and leaked a bogus
  // shell.openExternal to the OS on every Retry click).
  contents.on("will-navigate", async (event, url) => {
    if (url.startsWith("nh://retry")) {
      event.preventDefault();
      if ((await probe(ORIGIN)) === "up") await contents.loadURL(ORIGIN);
      return;
    }
    if (!isAppOrigin(url, ORIGIN)) {
      event.preventDefault();
      openExternallyIfWeb(url);
    }
  });
}

async function loadBoardOrError(w) {
  // E2: attach when the operator's server is up; otherwise spawn
  // `nh start --no-open` and wait. Failure renders error.html — never blank.
  serverState = await ensureServer({ origin: ORIGIN });
  if (serverState.status !== "failed") {
    // Belt to the server's no-cache header: revalidate the app shell
    // document on every launch (hashed assets still cache-hit).
    await w.loadURL(ORIGIN, { extraHeaders: "Cache-Control: no-cache" });
    return;
  }
  await w.loadFile(path.join(__dirname, "error.html"), {
    query: { origin: ORIGIN, reason: serverState.reason },
  });
}

async function createWindow() {
  win = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 720,
    minHeight: 480,
    title: "no_human",
    // Native-Mac chrome (operator: "Claude-desktop-grade look"): content
    // extends under a hidden-inset title bar; traffic lights float over the
    // sidebar's brand zone, which the web app makes draggable when it
    // detects the shell. Base color = the board's real --bg token.
    titleBarStyle: "hiddenInset",
    trafficLightPosition: { x: 18, y: 18 },
    // Pre-paint color follows the OS theme — a light-mode launch used to
    // flash dark before first paint (shell audit, 2026-07-17).
    backgroundColor: nativeTheme.shouldUseDarkColors ? "#0F1117" : "#F4F5F7",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.cjs"),
      // The board polls for fresh arrivals; a hidden window must keep ticking.
      backgroundThrottling: false,
    },
  });
  win.on("closed", () => { win = null; });
  // E3 (darwin): closing HIDES to the tray — the board keeps polling and
  // notifications keep coming. Quit is explicit: tray menu or Cmd-Q.
  win.on("close", (e) => {
    if (process.platform === "darwin" && !quitting) {
      e.preventDefault();
      win.hide();
    }
  });
  // Dock/taskbar badge mirrors the web app's own "(N) no_human" title —
  // the count the board derives with isNeedsYou. No IPC, no second truth.
  win.webContents.on("page-title-updated", (_e, title) => {
    const count = parseBadgeCount(title);
    if (count === null) return; // foreign title (error page) — keep the last truthful badge
    try { app.setBadgeCount(count); } catch { /* linux without libunity */ }
  });
  routeExternally(win.webContents);
  await loadBoardOrError(win);
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    // Guard the close→quit race (review finding: a destroyed BrowserWindow
    // reference throws in the main process).
    if (win && !win.isDestroyed()) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });
  app.whenReady().then(async () => {
    // Tray failure must never abort startup (review: a bad image on some
    // platforms throws here, and this runs BEFORE the window exists).
    try { buildTray(); } catch (err) { console.error("tray failed:", err); }
    await createWindow();
  });
  app.on("activate", () => showWindow());
  // E3: on darwin the app lives in the tray after window close; only an
  // explicit quit tears down. Elsewhere, all-windows-closed still quits.
  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });
  app.on("before-quit", () => {
    quitting = true;
    // E2 gating: stops ONLY a shell-spawned server; attached is untouched.
    if (stopServer(serverState)) serverState = null;
  });
}
