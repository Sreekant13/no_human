// E1 live smoke: launch the shell, assert the board (or error page) renders,
// assert external links are denied in-window, quit. Full e2e integration
// arrives in E4; this is the "it actually opens" proof.
// Run: node desktop/smoke.mjs   (needs web/node_modules' playwright)
import { _electron as electron } from "../web/node_modules/playwright/index.mjs";

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const here = new URL(".", import.meta.url).pathname;
const extLog = path.join(os.tmpdir(), `nh-smoke-ext-${process.pid}.log`);
fs.writeFileSync(extLog, "");
const app = await electron.launch({
  args: ["."],
  cwd: here,
  env: { ...process.env, NH_TEST_LOG: extLog },
  executablePath: `${here}node_modules/electron/dist/Electron.app/Contents/MacOS/Electron`,
});
try {
  const win = await app.firstWindow({ timeout: 15000 });
  await win.waitForLoadState("domcontentloaded");
  const url = win.url();
  const title = await win.title();
  const marker = await win.evaluate(() => window.nhDesktop?.shell === true);
  console.log(`window url=${url}`);
  console.log(`title=${title}`);
  console.log(`preload marker=${marker}`);
  // The board must ACTUALLY attach (review finding: accepting error.html
  // made the smoke pass without proving attachment). The live server is a
  // precondition of this smoke, like web/e2e/live-flows.
  if (!/^http:\/\/127\.0\.0\.1:\d+\/?$/.test(url)) {
    throw new Error(`board did not attach; window url: ${url}`);
  }
  // A native application menu must be set (a custom menu REPLACES the default,
  // so this proves setApplicationMenu ran and includes the View navigation).
  const menu = await app.evaluate(({ Menu }) => {
    const m = Menu.getApplicationMenu();
    return m ? m.items.map((i) => i.label || i.role) : null;
  });
  console.log(`app menu: ${JSON.stringify(menu)}`);
  if (!menu) throw new Error("no application menu was set");
  if (!menu.some((l) => /view/i.test(l))) throw new Error("app menu missing View");
  // show:false + ready-to-show must reveal the window, never strand it hidden.
  const visible = await app.evaluate(({ BrowserWindow }) => {
    const w = BrowserWindow.getAllWindows()[0];
    return Boolean(w && w.isVisible());
  });
  console.log(`window visible=${visible}`);
  if (!visible) throw new Error("window never became visible after ready-to-show");
  // External link must NOT create a second window.
  const before = app.windows().length;
  await win.evaluate(() => window.open("https://example.com/external"));
  await new Promise((r) => setTimeout(r, 1200));
  const after = app.windows().length;
  console.log(`windows before=${before} after=${after}`);
  if (after !== before) throw new Error("external link opened an in-app window");
  // Routing is observable, not assumed (review finding: deny-only assertion
  // was a tautology): the external URL must reach shell.openExternal; a
  // same-origin open must NOT.
  await win.evaluate((u) => window.open(u), url);
  await new Promise((r) => setTimeout(r, 800));
  const extLines = fs.readFileSync(extLog, "utf8").trim().split("\n").filter(Boolean);
  console.log(`openExternal log: ${JSON.stringify(extLines)}`);
  if (!extLines.some((l) => l.includes("example.com/external"))) {
    throw new Error("external link never reached shell.openExternal");
  }
  if (extLines.some((l) => l.includes("127.0.0.1"))) {
    throw new Error("same-origin open leaked to shell.openExternal");
  }
  console.log("SMOKE PASS");
} finally {
  await app.close();
}
