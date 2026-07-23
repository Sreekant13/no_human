// a11y guard for the Settings test-layer form's <select> controls.
// runner/gating/ci-backend selects had no accessible name (no <label>, no
// aria-label, and a <select> can't use placeholder), so a screen reader
// announced bare "combo box". This drives the real Settings UI and asserts each
// select is reachable by its accessible name. Mocked API, no :8420.
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
await new Promise((r) => srv.listen(4650, r));

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
const PROJECT = { id: "p1", name: "metrics-core", repo_paths: ["~/git/metrics-core"], test_layers: [] };
await page.route("**/api/**", (route) => {
  const u = route.request().url();
  const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
  if (u.includes("/api/onboarding")) return j({ completed: true });
  if (u.includes("/api/tasks")) return j([]);
  if (u.includes("/api/projects")) return j([PROJECT]);
  return j({});
});
await page.goto("http://127.0.0.1:4650/", { waitUntil: "networkidle" });
await page.waitForTimeout(400);

// Open Settings (opens on the Projects section by default).
await page.getByRole("button", { name: /^Settings$/ }).click();
await page.waitForTimeout(500);
check("Settings opened on Projects", await page.getByText("metrics-core").first().isVisible().catch(() => false));

// Expand the project card, then open the add-layer form.
await page.getByText("metrics-core").first().click();
await page.waitForTimeout(300);
const addLayer = page.getByRole("button", { name: /Add layer/i });
check("project expands to reveal the test-layer editor", await addLayer.isVisible().catch(() => false));
await addLayer.click();
await page.waitForTimeout(300);

// runner + gating selects are shown for the default (local) runner.
check("Test runner select has an accessible name",
  await page.getByLabel("Test runner").isVisible().catch(() => false));
check("Gating select has an accessible name",
  await page.getByLabel("Gating").isVisible().catch(() => false));

// Switching the runner to "ci" reveals the CI backend select.
await page.getByLabel("Test runner").selectOption("ci").catch(() => {});
await page.waitForTimeout(200);
check("CI backend select has an accessible name",
  await page.getByLabel("CI backend").isVisible().catch(() => false));

check("no page errors", errors.length === 0, errors[0] || "");

await ctx.close();
await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
