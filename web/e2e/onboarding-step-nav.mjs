// Clickable step indicator (spec §3 B2): the step row is buttons — clicking one
// jumps to that step, and ArrowLeft/ArrowRight move focus between them. Mocked
// API, no :8420.
import { chromium } from "playwright";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";

const DIST = new URL("../dist", import.meta.url).pathname;
const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };
const srv = http.createServer((q, r) => {
  const u = q.url.split("?")[0];
  let f = path.join(DIST, u === "/" ? "index.html" : u);
  if (!fs.existsSync(f) || fs.statSync(f).isDirectory()) f = path.join(DIST, "index.html");
  r.writeHead(200, { "Content-Type": MIME[path.extname(f)] || "application/octet-stream" });
  r.end(fs.readFileSync(f));
});
await new Promise((r) => srv.listen(4642, r));

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
await page.route("**/api/**", (route) => {
  const u = route.request().url();
  const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
  if (u.includes("/api/onboarding/status")) return j({ completed: false });
  if (u.includes("/api/repos/discover")) return j({
    repos: [], roots_scanned: [], roots: [], roots_missing: [], roots_refused: [],
    refused: [], home_direct: 0, total_found: 0, limit: 200, capped: false,
    walk_truncated: false, note: "", elapsed_ms: 1,
  });
  if (u.includes("/api/tasks")) return j([]);
  return j({});
});
await page.goto("http://127.0.0.1:4642/", { waitUntil: "networkidle" });
await page.waitForTimeout(400);

// Jump straight to the Projects step by clicking its step button — no Continue.
const projectsStep = page.getByRole("button", { name: /^Projects, step \d+ of \d+/ });
check("the step indicator exposes a 'Projects' step button", await projectsStep.isVisible().catch(() => false));
await projectsStep.click();
await page.waitForTimeout(300);
check("clicking the Projects step jumps to the Projects card",
  await page.getByRole("heading", { name: /Group repos into projects/i }).isVisible().catch(() => false));

// Roving focus: focus the first step, ArrowRight moves focus to the next step.
const welcomeStep = page.getByRole("button", { name: /^Welcome, step 1 of \d+/ });
await welcomeStep.focus();
await page.keyboard.press("ArrowRight");
await page.waitForTimeout(150);
const focusedLabel = await page.evaluate(() => document.activeElement?.getAttribute("aria-label") || "");
check("ArrowRight moves focus to the next step button", /step 2 of/.test(focusedLabel), `focused: ${focusedLabel}`);
await page.keyboard.press("ArrowLeft");
await page.waitForTimeout(150);
const backLabel = await page.evaluate(() => document.activeElement?.getAttribute("aria-label") || "");
check("ArrowLeft moves focus back", /step 1 of/.test(backLabel), `focused: ${backLabel}`);

check("no page errors during step navigation", errors.length === 0, errors[0] || "");

await ctx.close();
await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
