// no_human desktop shell — Phase E1 (attach-only).
//
// Points a native window at the LOCAL nh server (same-origin, so the web
// app's relative fetch / location.host WebSocket / SSE work untouched).
// External links (PR/CI URLs) open in the OS browser, never in-window.
// If the server isn't reachable, a friendly error page renders with retry —
// a blank window is the one unacceptable failure mode.

import { app, BrowserWindow, ipcMain, Menu, nativeImage, nativeTheme, shell, Tray } from "electron";
import { hasCredential, setAuthMode, writeCredential } from "./tokenStore.mjs";
import { isSetupUrl } from "./setupGate.mjs";
import { restartFailedMessage } from "./setupUi.mjs";
import {
  saveAction, stateOnProbeUp,
} from "./serverOwnership.mjs";
import { createNavScheduler } from "./navScheduler.mjs";
import { createServerLifecycle } from "./serverLifecycle.mjs";
import { quitAction } from "./quitPolicy.mjs";
import { parseBadgeCount } from "./badge.mjs";
import { buildMenuTemplate } from "./menu.mjs";
import { createUpdater } from "./updater.mjs";
import { updateMessage } from "./updatePolicy.mjs";
import { readUpdateState, writeUpdateState } from "./updateState.mjs";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  DEFAULT_ORIGIN,
  ensureServer,
  forceStopServer,
  isAppOrigin,
  probe,
  stopServer,
} from "./server.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ORIGIN = process.env.NH_ORIGIN || DEFAULT_ORIGIN;

let win = null;
// The ensure result lives in `lifecycle` (below) — ONE owner, because keeping a
// second copy here is how a spawned child got overwritten and orphaned.
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

export function serverLabel() {
  if (!lifecycle.state) return "server: probing…";
  return {
    attached: "server: attached (operator-owned)",
    spawned: "server: spawned by the app",
    failed: `server: unreachable (${lifecycle.state.reason})`,
  }[lifecycle.state.status] ?? "server: unknown";
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

// A native application menu — the operator-facing keyboard surface (view
// navigation + New Task + reload/zoom + dev-only devtools). Nav items post to
// the renderer over IPC ("nh:menu"); the preload forwards them to the board's
// own page state (App.jsx), so the menu drives the SAME UI, not a second one.
function sendToRenderer(action) {
  showWindow(); // a menu action on a tray-hidden window should surface it first
  if (win && !win.isDestroyed()) win.webContents.send("nh:menu", action);
}

// ------------------------------- updates -------------------------------- //
//
// Whether this build may install its own updates is decided at BUILD time, not
// here: `process.env.CSC_LINK` is meaningless inside a shipped app, so
// electron-builder.config.cjs stamps the answer into the packaged
// package.json. Reading the live environment instead would let any user turn
// on an update path macOS will refuse.
export function packagedSigning() {
  try {
    const pkg = JSON.parse(
      fs.readFileSync(path.join(__dirname, "package.json"), "utf8"));
    return {
      mode: pkg.nhSigning || "unsigned",
      // Strict true, so a missing or malformed field fails CLOSED.
      canAutoUpdate: pkg.nhCanAutoUpdate === true,
    };
  } catch {
    return { mode: "unsigned", canAutoUpdate: false };
  }
}

let updater = null;

// electron-updater is imported LAZILY and never at module scope: it touches
// app.getVersion() and app-update.yml on import, which throws in an unpackaged
// run and makes main.mjs unimportable under the test loader.
async function getUpdater() {
  if (updater) return updater;
  let autoUpdater;
  try {
    const mod = await import("electron-updater");
    autoUpdater = (mod.default ?? mod).autoUpdater;
  } catch (err) {
    console.error("electron-updater unavailable:", (err && err.message) || err);
    return null;
  }
  if (!autoUpdater) return null;
  const dir = app.getPath("userData");
  updater = createUpdater({
    autoUpdater,
    plan: packagedSigning(),
    currentVersion: app.getVersion(),
    isPackaged: app.isPackaged,
    readState: () => readUpdateState(dir),
    writeState: (s) => writeUpdateState(dir, s),
    onEvent: (payload) => sendUpdateEvent(payload),
    log: (m) => console.log(`[update] ${m}`),
  });
  updater.configure();
  return updater;
}

// The board renders the notice; the shell never puts a modal in the way. An
// update is information, not an interruption.
function sendUpdateEvent(payload) {
  if (win && !win.isDestroyed()) {
    win.webContents.send("nh:update", {
      ...payload,
      message: updateMessage({
        mode: payload.mode, latest: payload.latest,
        current: payload.current ?? app.getVersion(),
        canAutoUpdate: payload.canAutoUpdate,
      }),
    });
  }
}

async function checkForUpdates({ manual = false } = {}) {
  const u = await getUpdater();
  if (!u) {
    if (manual) {
      sendUpdateEvent({ mode: "failed",
        error: "The updater component is not available in this build." });
    }
    return null;
  }
  // Never allowed to reject: a failed update check must not surface as a
  // startup error or an unhandled rejection.
  return u.check({ manual }).catch((err) => {
    console.error("update check failed:", (err && err.message) || err);
    return null;
  });
}

// The SECOND route to documentation, not the only one: the bundle ships its
// own docs as `extraResources` (`../docs/quickstart.md` and
// `../docs/configuration.md` → `Contents/Resources/docs/`, declared in
// electron-builder.config.cjs), and `bundledDoc()` below resolves them under
// `process.resourcesPath` with a repo-relative fallback for dev runs. Help
// offers the bundled copy first and this URL second — see menu.mjs.
//
// CANONICAL, not `/docs.html` — that form only reaches the page through a 307,
// and the site's own markup links `/docs` in all five places. A redirect is a
// thing someone eventually retires.
const DOCS_URL = "https://getnohuman.com/docs";

/** Absolute path to a doc shipped inside the bundle, or null when unpackaged.
 *
 * `extraResources` land in `Contents/Resources`, which is `process.resourcesPath`
 * in a packaged app. In a dev run there is no bundle, so the repo's own `docs/`
 * is used — otherwise the menu item would be dead for every developer and the
 * one person able to notice it broken would never see it.
 */
function bundledDoc(name) {
  const base = app.isPackaged
    ? path.join(process.resourcesPath, "docs")
    : path.join(__dirname, "..", "docs");
  const p = path.join(base, `${name}.md`);
  return fs.existsSync(p) ? p : null;
}

function buildAppMenu() {
  const template = buildMenuTemplate({
    isMac: process.platform === "darwin",
    isDev: !app.isPackaged,
    onNavigate: (page) => sendToRenderer(page),
    onNewTask: () => sendToRenderer("new-task"),
    onCheckForUpdates: () => {
      showWindow();
      checkForUpdates({ manual: true });
    },
    onReenterToken: async () => {
      showWindow();
      if (win && !win.isDestroyed()) {
        await openSetup(win).catch((err) =>
          console.error("setup screen failed:", err));
      }
    },
    // Routed through openExternallyIfWeb, NOT shell.openExternal directly. That
    // helper is the one place this process hands a URL to the OS, and it
    // refuses anything that is not http(s); going around it for a URL that
    // "is obviously fine" is how that guard stops being the one place.
    //
    // It points at the site rather than the GitHub repository on purpose: the
    // repository is private until the operator makes it public, and a link that
    // 404s for every user is worse than no link at all. Revisit when it opens.
    onOpenDocs: (which) => {
      if (which === "site") return openExternallyIfWeb(DOCS_URL);
      // Falls back to the site when the bundled copy is missing rather than
      // doing nothing: a Help item that silently no-ops is worse than one that
      // opens the wrong-ish page, because the user cannot tell it tried.
      const p = bundledDoc("quickstart");
      return p ? shell.openPath(p) : openExternallyIfWeb(DOCS_URL);
    },
  });
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
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
      // Retry is a plain link and can be clicked repeatedly; each accepted
      // click is a full ensureServer AND another spawned nh, so it runs only
      // when idle (navScheduler owns that rule, and tests it).
      // Through loadBoardOrError so lifecycle.state is updated: loading the URL
      // directly left a stale "failed" state and the tray read
      // "server: unreachable" while the board was live.
      if (win && !win.isDestroyed()) {
        // May reject (nav timeout); an async listener must not leak that.
        await (navs.scheduleIfIdle(win) ?? Promise.resolve())
          .catch((err) => console.error("retry failed:", err));
      }
      return;
    }
    if (url.startsWith("nh://token")) {
      event.preventDefault();
      if (win && !win.isDestroyed()) {
        await openSetup(win).catch((err) =>
          console.error("setup screen failed:", err));
      }
      return;
    }
    if (!isAppOrigin(url, ORIGIN)) {
      event.preventDefault();
      openExternallyIfWeb(url);
    }
  });
}

// Server ownership + restart live in serverLifecycle.mjs so they can be driven
// under `node --test` without electron — both of the last two blocking defects
// were in this code while it was inline and untested.
const lifecycle = createServerLifecycle({
  probe: () => probe(ORIGIN),
  stopServer,
  forceStopServer,
  sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
});
const rememberChild = (child) => lifecycle.track(child);
const stopAllOwned = () => lifecycle.stopAll();

// Navigations are SERIALIZED and supersedable — see navScheduler.mjs, which
// owns both behaviours and is unit-tested (this logic went wrong twice inline).
const navs = createNavScheduler((w, isCurrent) => _loadBoardOrError(w, isCurrent));
const loadBoardOrError = (w) => navs.schedule(w);
const supersedeNav = () => navs.supersede();

const showError = (w, reason, detail) => w.loadFile(path.join(__dirname, "error.html"), {
  query: {
    origin: ORIGIN, reason, packaged: app.isPackaged ? "1" : "0",
    ...(detail ? { detail } : {}),
  },
});

/**
 * Load the board, and never leave a BLANK window if that fails. A probe can
 * answer moments before the server dies (see restartOwnServer's note), and a
 * rejected loadURL both blanks the window and throws an unhandled rejection in
 * the main process — "a blank window is the one unacceptable failure mode".
 */
async function showBoard(w) {
  try {
    await w.loadURL(ORIGIN, { extraHeaders: "Cache-Control: no-cache" });
    return true;
  } catch (err) {
    const msg = String((err && err.message) || err);
    // ERR_ABORTED means a NEWER load replaced this one — opening the credential
    // screen over an in-flight board load does exactly that. It is not a
    // failure, and rendering the error page here would paint over the screen the
    // user just moved to.
    if (/ERR_ABORTED/.test(msg)) return false;
    console.error("board load failed:", msg);
    // The fallback can fail too (window torn down mid-load); swallowing that is
    // correct — there is nothing left to render onto.
    try { await showError(w, "load-failed"); } catch { /* window is gone */ }
    return false;
  }
}

async function _loadBoardOrError(w, current) {
  // Probe BEFORE anything else: a server the operator already started is
  // authoritative, and it may hold its token in its own environment rather than
  // in ~/.no_human/.env. Prompting there would nag about a credential while a
  // healthy board sits one probe away.
  // heldStopFailed(): we are holding a server we could not stop, so a probe-up
  // is THAT server, still on the OLD token. Falling through to the restart path
  // means a Retry genuinely re-attempts the stop instead of quietly attaching.
  if ((await probe(ORIGIN)) === "up" && !lifecycle.heldStopFailed()) {
    if (!current()) return;           // don't publish state for a dead nav
    // Keeps a spawned state (and its child) intact — see serverOwnership.
    lifecycle.state = stateOnProbeUp(lifecycle.state);
    await showBoard(w);
    return;
  }
  // A first launch has no credential, and `nh start` exits rather than boot
  // without one — so the board (and its web onboarding) is unreachable until a
  // token exists. Ask for it natively: pointing the operator at a terminal, as
  // error.html does, is a dead end in a packaged app.
  if (!hasCredential()) {
    if (!current()) return;
    await showSetup(w);
    return;
  }
  // E2: spawn `nh start --no-open` and wait. Failure renders error.html —
  // never blank.
  // Nothing below is free: ensureServer boots a real server (SQLite, workers,
  // a port bind). A nav that is already superseded must not do it — the
  // discarded process went on to win the port with the OLD token and dead-ended
  // the credential screen.
  if (!current()) return;
  // At most ONE owned server. A previous attempt's child is still running (and
  // still failing), so starting another just adds a process racing for the same
  // port — repeated Retry accumulated live servers. Stop it and wait for the
  // port before replacing it.
  if (lifecycle.ownsAny() && !(await restartOwnServer())) {
    // We could not stop our own server. Falling through to ensureServer here
    // attached to it, so the board ran on the OLD token, the tray called it
    // "operator-owned", and the handle was gone — unstoppable forever.
    // Keeps the child — see serverLifecycle.failedStopState().
    lifecycle.state = lifecycle.failedStopState();
    if (!current()) return;
    await showError(w, lifecycle.state.reason);
    return;
  }
  if (!current()) return;             // the stop above can take up to 20s
  // Register at SPAWN, not on return: for the ~20s until ensureServer resolves
  // the child was in no registry, so nothing could stop it and a token save
  // reported success over a server holding the old credential.
  const result = await ensureServer({
    origin: ORIGIN,
    onSpawn: (child) => rememberChild(child),
  });
  if (!current()) return;             // superseded while we waited
  lifecycle.state = result;
  if (lifecycle.state.status !== "failed") {
    await showBoard(w);
    return;
  }
  await showError(w, lifecycle.state.reason, lifecycle.state.detail);
}

const SETUP_FILE = path.join(__dirname, "token.html");

/**
 * Stop the server this shell started and WAIT for the port to actually free.
 * Killing and probing immediately raced a graceful shutdown: the probe still
 * answered, so the shell reported "Connected" and attached to a server that
 * died moments later. Returns false if it never goes down.
 */
const restartOwnServer = (timeoutMs = 20000) => lifecycle.restart(timeoutMs);

/**
 * Open the credential screen, deciding for itself whether a board is reachable
 * behind it. BOTH entry points (the File menu and nh://token) go through here:
 * when nh://token hardcoded canReturn:false, Escape quit the app even though a
 * board was live — N1's failure reached through the other door.
 */
async function openSetup(w) {
  const up = (await probe(ORIGIN)) === "up";
  supersedeNav();      // an in-flight nav must not paint over this screen
  // Deliberately OFF the nav queue: queueing this behind a 20s ensureServer
  // would make the menu item feel dead. The cost is that it can cancel an
  // in-flight loadURL, which rejects with ERR_ABORTED — expected, not an error.
  try {
    await showSetup(w, { canReturn: up });
  } catch (err) {
    const msg = String((err && err.message) || err);
    if (/ERR_ABORTED/.test(msg)) return;     // superseded — something newer paints
    // The credential screen itself failed to load. token.html HAS been missing
    // from app.asar before, and both callers only log, so rethrowing here left
    // a BLANK window — the one unacceptable failure mode. Give this door the
    // same net the startup path has. (Retry on that page re-opens this screen,
    // which is a legitimate retry rather than a dead end.)
    console.error("setup screen failed:", msg);
    await showError(w, "setup-failed").catch(() => {});
  }
}

/**
 * Show the credential screen. `canReturn` tells it whether a board exists
 * behind it: on genuine first run there is nothing to dismiss to and the
 * secondary action quits, but when reached from File > Re-enter Claude Token
 * over a working board, quitting would tear down the app (and stop a
 * shell-spawned server) on one unconfirmed keystroke.
 */
async function showSetup(w, { canReturn = false } = {}) {
  await w.loadFile(SETUP_FILE, canReturn ? { query: { canReturn: "1" } } : {});
}

/**
 * The setup screen shares ONE BrowserWindow (and therefore one preload) with
 * the board, so `window.nhSetup` is reachable from every page the server
 * renders. Gate on the sender actually BEING the local setup file: without
 * this, anything injected into the board could overwrite the operator's token
 * or quit the app. Same standard as openExternallyIfWeb — the shell does not
 * trust the board's content.
 */
function fromSetupScreen(event) {
  return isSetupUrl(event.senderFrame?.url ?? "", SETUP_FILE);
}

// token.html -> main. Returns {ok} or {error}; the value is never echoed back
// and never logged. `mode` is the credential type the operator selected —
// anything but the explicit "api_key" opt-in is treated as the default, so a
// stale renderer that omits it keeps today's behaviour.
ipcMain.handle("nh:save-token", async (event, value, mode) => {
  if (!fromSetupScreen(event)) return { ok: false, error: "not permitted" };
  const m = mode === "api_key" ? "api_key" : "subscription";
  try {
    // Credential first: if it fails validation, the configured mode is
    // untouched and the screen still matches config.yaml.
    writeCredential(value, m);
    setAuthMode(m);
  } catch (err) {
    return { ok: false, error: err.message };
  }
  // A running server read its token ONCE at bootstrap, so writing a new one is
  // inert until that process restarts. Silently returning to the board would
  // report success while every task keeps failing on the old credential.
  // ownsAnything covers a boot still in flight: saveAction on lifecycle.state alone
  // saw "nothing running" mid-boot and returned "proceed", so the user was told
  // "Connected" over a server started with the old token.
  // ONE predicate drives both the branch and the message. Deriving them from
  // different sources is what produced the last two blockers, in mirror image.
  const alive = lifecycle.ownsAnyAlive();
  const action = saveAction(lifecycle.state, (await probe(ORIGIN)) === "up", alive);
  if (action === "needs-restart") {
    return { ok: false, needsRestart: true, error:
      "Saved. The no_human server was already running and still holds the old " +
      "token — restart it (quit `nh start` and run it again) to use the new one." };
  }
  if (action === "restart" && !(await restartOwnServer())) {
    // Same `alive` that chose the branch — see ownsAnyAlive().
    return { ok: false, error: restartFailedMessage(ORIGIN, alive) };
  }
  try {
    if (win && !win.isDestroyed()) await loadBoardOrError(win);
  } catch (err) {
    // Never leave the setup screen wedged on "Saving…": report and let the
    // user retry rather than stranding a disabled button.
    return { ok: false, error: `Saved, but the board did not open: ${err.message}` };
  }
  // The nav ran, but "ran" is not "started". Reporting ok here painted
  // "Connected. Opening no_human…" over an error page.
  if (lifecycle.state?.status === "failed") {
    return { ok: false, error:
      `Saved, but the server did not start (${lifecycle.state.reason}).` };
  }
  return { ok: true };
});

// Dismiss back to the board — only meaningful when one is reachable.
ipcMain.handle("nh:dismiss", async (event) => {
  if (!fromSetupScreen(event)) return false;
  if (win && !win.isDestroyed()) await loadBoardOrError(win);
  return true;
});

ipcMain.handle("nh:quit", (event) => {
  if (!fromSetupScreen(event)) return false;
  quitting = true;
  app.quit();
  return true;
});

// Update IPC. Deliberately NOT gated by fromSetupScreen: that predicate admits
// only token.html, and these are driven by the BOARD. They are safe to expose
// there because none of them accepts a URL, a path, or a credential — the feed
// is fixed at build time and the only inputs are "yes", "later", and "restart".
ipcMain.handle("nh:update-check", async () => {
  const r = await checkForUpdates({ manual: true });
  return r ?? { mode: "failed", error: "the updater is unavailable" };
});

ipcMain.handle("nh:update-download", async () => {
  const u = await getUpdater();
  if (!u) return { mode: "failed", error: "the updater is unavailable" };
  return u.download();
});

ipcMain.handle("nh:update-install", async () => {
  const u = await getUpdater();
  if (!u) return { mode: "failed", error: "the updater is unavailable" };
  // Refuse before latching `quitting`: if nothing is downloaded the app keeps
  // running, and a latched flag would turn the next window-close into a real
  // quit instead of hide-to-tray.
  if (!u.downloaded) return { mode: "failed", error: "no update has been downloaded" };
  // Quitting for an install is a real quit, not a hide-to-tray. The flag must
  // be set BEFORE install() — quitAndInstall fires the quit path immediately.
  quitting = true;
  return u.install();
});

ipcMain.handle("nh:update-defer", async (_event, version) => {
  const u = await getUpdater();
  if (!u) return { mode: "failed", error: "the updater is unavailable" };
  return u.defer(version);
});

async function createWindow() {
  const dark = nativeTheme.shouldUseDarkColors;
  // Pre-paint color follows the OS theme — a light-mode launch used to flash
  // dark before first paint (shell audit, 2026-07-17).
  const bg = dark ? "#0F1117" : "#F4F5F7";
  win = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 720,
    minHeight: 480,
    title: "no_human",
    // Present only once the first frame is ready (below) — a window shown
    // eagerly paints a bg-colored rectangle before content, the classic flash.
    show: false,
    backgroundColor: bg,
    // Native-Mac chrome (operator: "Claude-desktop-grade look"): content
    // extends under a hidden-inset title bar; traffic lights float over the
    // sidebar's brand zone, which the web app makes draggable when it detects
    // the shell. On Windows, `hiddenInset` degrades to a frameless window with
    // NO controls — use `hidden` + a themed titleBarOverlay so the min/max/
    // close buttons come back (untestable on this Mac; guarded by platform).
    ...(process.platform === "darwin"
      ? { titleBarStyle: "hiddenInset", trafficLightPosition: { x: 18, y: 18 } }
      : {}),
    ...(process.platform === "win32"
      ? { titleBarStyle: "hidden",
          titleBarOverlay: { color: bg, symbolColor: dark ? "#E6E8EC" : "#1A1D23", height: 40 } }
      : {}),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.cjs"),
      // The board polls for fresh arrivals; a hidden window must keep ticking.
      backgroundThrottling: false,
    },
  });
  // Reveal on the first paint — with show:false above this kills the flash and
  // never leaves the window hidden: ready-to-show fires for the board AND for
  // error.html (loadBoardOrError always loads one of them).
  win.once("ready-to-show", () => { if (win && !win.isDestroyed()) win.show(); });
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
    buildAppMenu();
    await createWindow();
    // AFTER the window exists (it is the thing that renders the notice) and
    // deliberately NOT awaited: a slow or dead update feed must never delay the
    // board appearing. Throttled to once a day inside the updater.
    checkForUpdates().catch(() => {});
  }).catch((err) => {
    // Startup must never end in an unhandled rejection: that leaves a blank
    // window with no error page and no Retry.
    console.error("startup failed:", (err && err.message) || err);
    if (win && !win.isDestroyed()) showError(win, "startup-failed").catch(() => {});
  });
  app.on("activate", () => showWindow());
  // E3: on darwin the app lives in the tray after window close; only an
  // explicit quit tears down. Elsewhere, all-windows-closed still quits.
  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });
  // Quitting is DELAYED so a server that ignores SIGTERM can be escalated to
  // SIGKILL instead of outliving the app and holding the port. quitPolicy owns
  // the decision (and is tested): a SECOND Cmd-Q during the delay must hold
  // again, not fall through and abandon the escalation.
  let shuttingDown = false;
  let shutdownDone = false;
  app.on("before-quit", (event) => {
    quitting = true;
    const action = quitAction({
      ownsAny: lifecycle.ownsAny(), shuttingDown, shutdownDone });
    if (action === "keep-held") { event.preventDefault(); return; }
    if (action === "delay") {
      shuttingDown = true;
      event.preventDefault();
      // Hard ceiling: a shutdown that never settles must not make the app
      // un-quittable.
      const hardExit = setTimeout(() => { shutdownDone = true; app.exit(0); }, 20000);
      lifecycle.shutdown()
        .catch((err) => console.error("shutdown failed:", err))
        .finally(() => {
          clearTimeout(hardExit);
          shutdownDone = true;
          app.quit();
        });
      return;
    }
    // E2 gating: stops ONLY a shell-spawned server; attached is untouched.
    // Fallback for the non-owning path (nothing of ours is running).
    stopAllOwned();
    stopServer(lifecycle.state);
    lifecycle.state = null;
  });
}
