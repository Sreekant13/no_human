// E4: the desktop shell in the standing UI gate. Launches the real Electron
// app against the live server (same precondition as live-flows), read-only.
// SKIPS VISIBLY when electron isn't installed so the base gate stays light.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DESKTOP = path.resolve(HERE, "..", "..", "desktop");
const ELECTRON = path.join(
  DESKTOP, "node_modules", "electron", "dist",
  "Electron.app", "Contents", "MacOS", "Electron");

if (!fs.existsSync(ELECTRON)) {
  console.log("SKIP: electron not installed (cd desktop && npm install)");
  process.exit(0);
}

const { _electron: electron } = await import("playwright");

const extLog = path.join(os.tmpdir(), `nh-e2e-ext-${process.pid}.log`);
fs.writeFileSync(extLog, "");
const app = await electron.launch({
  args: ["."], cwd: DESKTOP,
  env: { ...process.env, NH_TEST_LOG: extLog },
  executablePath: ELECTRON,
});
try {
  const win = await app.firstWindow({ timeout: 20000 });
  // Read-only discipline: hard-block every non-GET before interacting.
  await win.route("**/*", (route) => {
    const m = route.request().method();
    return (m === "GET" || m === "HEAD") ? route.continue() : route.abort();
  });
  await win.waitForLoadState("domcontentloaded");
  await win.waitForTimeout(2000);

  const url = win.url();
  if (!/^http:\/\/127\.0\.0\.1:\d+\/?$/.test(url)) {
    throw new Error(`board did not attach (url: ${url}) — is nh start up?`);
  }
  // The shell marker + the in-shell chrome accommodations must be live.
  const marker = await win.evaluate(() => window.nhDesktop?.shell === true);
  if (!marker) throw new Error("preload marker missing");
  const inShell = await win.evaluate(() =>
    document.documentElement.classList.contains("nh-in-shell"));
  if (!inShell) throw new Error("nh-in-shell class missing (stale bundle? cache?)");
  const pad = await win.evaluate(() => getComputedStyle(
    document.querySelector(".nh-sidebar-brand")).paddingTop);
  if (pad !== "34px") throw new Error(`traffic-light clearance not applied: ${pad}`);

  // External links: denied in-window, observed reaching (log-only) openExternal.
  const before = app.windows().length;
  await win.evaluate(() => window.open("https://example.com/x"));
  await win.waitForTimeout(800);
  if (app.windows().length !== before) throw new Error("external opened in-app window");
  const log = fs.readFileSync(extLog, "utf8");
  if (!log.includes("example.com/x")) throw new Error("external never reached openExternal");
  if (log.includes("127.0.0.1")) throw new Error("same-origin leaked to openExternal");

  console.log("ALL CHECKS PASSED");
} finally {
  await app.close();
  fs.rmSync(extLog, { force: true });
}
