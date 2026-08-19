// a11y guard for the onboarding flow's icon-only remove buttons.
// The "✕" remove controls (remove-project in the Projects step, remove-entry in
// the Docs ListEditor) render only glyph content; without an accessible name a
// screen reader announces nothing meaningful. This drives the real flow in a
// browser and asserts each carries an aria-label. Mocked API, no :8420.
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
await new Promise((r) => srv.listen(4640, r));

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
// Onboarding renders only when status is NOT completed.
await page.route("**/api/**", (route) => {
  const u = route.request().url();
  const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
  if (u.includes("/api/onboarding/status")) return j({ completed: false });
  if (u.includes("/api/onboarding/repos/detect")) return j({ repos: [] });
  if (u.includes("/api/tasks")) return j([]);
  return j({});
});
await page.goto("http://127.0.0.1:4640/", { waitUntil: "networkidle" });
await page.waitForTimeout(400);

const cont = () => page.getByRole("button", { name: /^Continue$/ }).click();

// Walk Continues until the Projects step's heading renders (bounded): the
// suite used to hardcode 3 Continues for a welcome->team->repos->projects
// order, and the step list has changed shape once already (the team step is
// gone) — counting steps is exactly what went stale (2026-08-19 fix).
for (let hop = 0; hop < 6; hop++) {
  if (await page.getByRole("heading", { name: /Group repos into projects/i })
      .isVisible().catch(() => false)) break;
  await cont(); await page.waitForTimeout(250);
}

// Projects step: add a project, then the remove-project ✕ appears.
const NAME = "metrics-core";
const projInput = page.getByPlaceholder(/Project name/i);
check("reached the Projects step",
  await projInput.waitFor({ state: "visible", timeout: 5000 }).then(() => true).catch(() => false));
await projInput.fill(NAME);
await projInput.press("Enter");
await page.waitForTimeout(300);
const removeProj = page.getByRole("button", { name: `Remove project ${NAME}` });
check("remove-project button has a descriptive aria-label",
  await removeProj.isVisible().catch(() => false),
  `looked for aria-label "Remove project ${NAME}"`);

// Docs step: ListEditor. Ensure 2+ entries so a remove ✕ renders, then check it.
await cont(); await page.waitForTimeout(300);
const addAnother = page.getByRole("button", { name: /add another/i });
check("reached the Docs step (ListEditor present)", await addAnother.isVisible().catch(() => false));
await addAnother.click();
await page.waitForTimeout(200);
// Empty entries -> the fallback label "Remove this entry".
const removeEntry = page.getByRole("button", { name: /^Remove this entry$/ });
check("ListEditor remove button has an aria-label",
  (await removeEntry.count()) >= 1,
  `found ${await removeEntry.count()} labelled remove button(s)`);

check("no page errors during onboarding", errors.length === 0, errors[0] || "");

await ctx.close();
await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
