// Task 5C step 1 — capture every flow of the LIVE app (real data, real bundle).
// READ-ONLY: navigation and drawer tabs only. Never clicks approve/cancel/retry/
// pause/reply/send-back — this drives the operator's real board.
import { chromium } from "playwright";
import fs from "node:fs";

const BASE = "http://127.0.0.1:8420";
const OUT = new URL("./shots", import.meta.url).pathname;
fs.mkdirSync(OUT, { recursive: true });

const DESTRUCTIVE = /approve|cancel|retry|pause|resume|send.?back|reply|merge|post|confirm|reject|delete|remove/i;
const report = [];
const fatal = [];

const browser = await chromium.launch();

async function capture(name, viewport, theme, drive) {
  const ctx = await browser.newContext({ viewport });
  const page = await ctx.newPage();
  const errors = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  // A ReferenceError in a .jsx renders a BLANK page and is invisible to `node --test`
  // (nothing in this repo mounts a component) and to `vite build` (it bundles an undeclared
  // identifier happily). Driving the built bundle is the ONLY thing that catches it — so a
  // page error is a hard failure, not a count.
  page.on("pageerror", (e) => { errors.push("PAGEERROR: " + e.message); fatal.push(e.message); });
  // Hard guard: block any non-GET request, so a stray click can never mutate state.
  await page.route("**/*", (route) => {
    const m = route.request().method();
    if (m !== "GET") {
      console.log(`  !! BLOCKED a ${m} ${route.request().url()} — driver must stay read-only`);
      return route.abort();
    }
    route.continue();
  });
  await page.addInitScript((t) => localStorage.setItem("nh-theme", t), theme);
  await page.goto(BASE, { waitUntil: "networkidle" });
  await page.waitForTimeout(600);

  await drive(page);
  await page.waitForTimeout(400);
  const file = `${OUT}/${name}-${theme}${viewport.width < 500 ? "-mobile" : ""}.png`;
  await page.screenshot({ path: file, fullPage: viewport.width >= 500 });
  report.push({ flow: name, theme, w: viewport.width, errors: errors.length, detail: errors.slice(0, 2).join(" | ") });
  console.log(`${errors.length ? "ERR " : "ok  "} ${name} [${theme}${viewport.width < 500 ? "/mobile" : ""}]${errors.length ? "  " + errors[0].slice(0, 90) : ""}`);
  await ctx.close();
}

const DESKTOP = { width: 1440, height: 950 };
const MOBILE = { width: 390, height: 844 };

const flows = {
  board: async () => {},
  "board-drawer": async (page) => {
    const card = page.locator(".task-card").first();
    if (await card.count()) {
      await card.click();
      await page.waitForTimeout(900);
    }
  },
  stats: async (page) => {
    await page.getByRole("button", { name: /^Stats$/ }).click();
    await page.waitForTimeout(1500);
  },
  settings: async (page) => {
    await page.getByRole("button", { name: /^Settings$/ }).click();
    await page.waitForTimeout(1200);
  },
  composer: async (page) => {
    await page.keyboard.press("n");
    await page.waitForTimeout(600);
  },
};

for (const [name, drive] of Object.entries(flows)) {
  for (const theme of ["dark", "light"]) {
    await capture(name, DESKTOP, theme, drive);
  }
  await capture(name, MOBILE, "dark", drive);
}

await browser.close();
if (fatal.length) {
  console.log(`\n*** ${fatal.length} PAGE ERROR(S) — the app crashed on some flow ***`);
  for (const f of [...new Set(fatal)]) console.log("   " + f);
}
console.log("\n=== console errors by flow ===");
console.table(report.filter((r) => r.errors > 0));
console.log(`captured ${report.length} shots → ${OUT}`);
if (fatal.length) process.exit(1);
console.log("ALL FLOWS CLEAN");
