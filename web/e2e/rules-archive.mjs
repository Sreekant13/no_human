// Rules/Skills UI: archived/superseded badge, "Show archived" filter, and the
// Restore/Dismiss triage actions (Memory lifecycle C part B). Drives the
// built bundle against a mocked /api/** — never touches :8420 — because
// `node --test` never mounts a component (memoryArchive.test.mjs proves the
// pure logic; this proves the DOM actually renders it — see run-all.mjs's
// header comment on the blank-page class of bug neither unit tests nor a
// clean `vite build` can catch).
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
await new Promise((r) => srv.listen(4671, r));

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};

const browser = await chromium.launch();

const LIVE_ID = "11111111-live-rule";
const ARCH_ID = "22222222-archived-rule";
const SUP_ID = "33333333-superseded-rule";

function baseRows() {
  return [
    { id: LIVE_ID, type: "rule", title: "Live rule", content: "keep me",
      tags: "[]", archived: 0, superseded_by: null, use_count: 0 },
    { id: ARCH_ID, type: "rule", title: "Archived rule", content: "old, unconfirmed too long",
      tags: "[]", archived: 1, superseded_by: null, use_count: 0 },
    { id: SUP_ID, type: "rule", title: "Superseded rule", content: "dup of the live one",
      tags: "[]", archived: 1, superseded_by: LIVE_ID, use_count: 0 },
  ];
}

// Open Settings and switch to the Rules section.
async function openRules(page) {
  await page.goto("http://127.0.0.1:4671/", { waitUntil: "networkidle" });
  await page.waitForTimeout(300);
  await page.getByRole("button", { name: /^Settings$/ }).click();
  await page.waitForTimeout(300);
  await page.getByRole("button", { name: /^Rules$/ }).click();
  await page.waitForTimeout(300);
}

// Installs the mocked API. `rows` is a live array (mutated by the /restore
// handler) so a refetch after a triage action observes the new state — the
// same contract the real server gives (a field flip, not a create/replace).
async function installRoutes(page, rows) {
  await page.route("**/api/**", (route) => {
    const req = route.request();
    const url = req.url();
    const method = req.method();
    const j = (b, s = 200) => route.fulfill({ status: s, contentType: "application/json", body: JSON.stringify(b) });
    if (url.includes("/api/onboarding")) return j({ completed: true });
    if (url.includes("/api/tasks")) return j([]);
    if (url.includes("/api/projects")) return j([]);
    if (url.includes("/api/memories/quarantine")) return j({ rules: 0, skills: 0 });
    const restoreMatch = url.match(/\/api\/learnings\/([^/]+)\/restore$/);
    if (restoreMatch && method === "POST") {
      const id = restoreMatch[1];
      const row = rows.find((r) => r.id === id);
      if (!row) return j({ detail: "not found" }, 404);
      if (!row.archived) return j({ ok: true, id, already_active: true });
      row.archived = 0;
      row.superseded_by = null;
      return j({ ok: true, id });
    }
    if (url.includes("/api/rules")) return j(rows);
    if (url.includes("/api/skills")) return j([]);
    return j({});
  });
}

// ── Scenario (a): default view — archived/superseded hidden, no badge ───────
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await installRoutes(page, baseRows());
  await openRules(page);

  check("[a] the live card is shown", await page.locator(".memory-card", { hasText: "Live rule" }).isVisible().catch(() => false));
  check("[a] the archived card is NOT shown by default", (await page.locator(".memory-card", { hasText: "Archived rule" }).count()) === 0);
  check("[a] the superseded card is NOT shown by default", (await page.locator(".memory-card", { hasText: "Superseded rule" }).count()) === 0);
  check("[a] no badge is rendered anywhere", (await page.locator(".memory-card-badge").count()) === 0);
  check("[a] the 'Show archived' toggle names the archived count (2)",
    (await page.locator(".memory-archive-toggle").innerText().catch(() => "")).includes("2"));
  check("[a] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── Scenario (b): "Show archived" reveals both, with the right badge text ───
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await installRoutes(page, baseRows());
  await openRules(page);

  await page.locator(".memory-archive-toggle input[type=checkbox]").check();
  await page.waitForTimeout(200);

  check("[b] all three cards are visible", (await page.locator(".memory-card").count()) === 3);
  const archBadge = page.locator(".memory-card", { hasText: "Archived rule" }).locator(".memory-card-badge");
  const supBadge = page.locator(".memory-card", { hasText: "Superseded rule" }).locator(".memory-card-badge");
  // textContent (not innerText): the badge's CSS renders it uppercase like
  // the sibling .memory-card-type badge, which innerText would reflect and
  // this assertion does not care about — it checks the literal DOM text
  // archiveBadge() produced, not its visual presentation.
  check("[b] the archived-only row is badged 'Archived'", (await archBadge.textContent().catch(() => "")) === "Archived");
  check("[b] the superseded row is badged 'Superseded', not 'Archived'", (await supBadge.textContent().catch(() => "")) === "Superseded");
  check("[b] the superseded badge names the survivor id", (await supBadge.getAttribute("title").catch(() => "") || "").includes(LIVE_ID.slice(0, 8)));
  check("[b] the live card carries no badge", (await page.locator(".memory-card", { hasText: "Live rule" }).locator(".memory-card-badge").count()) === 0);
  check("[b] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── Scenario (c): Restore — POST fires, refetch clears the badge ────────────
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const rows = baseRows();
  let restorePosted = null;
  page.on("request", (req) => {
    if (req.method() === "POST" && req.url().includes(`/api/learnings/${ARCH_ID}/restore`)) {
      restorePosted = req.url();
    }
  });
  await installRoutes(page, rows);
  await openRules(page);

  await page.locator(".memory-archive-toggle input[type=checkbox]").check();
  await page.waitForTimeout(200);

  const archCard = page.locator(".memory-card", { hasText: "Archived rule" });
  await archCard.getByRole("button", { name: "Restore" }).click();
  await page.waitForTimeout(400);

  check("[c] the Restore click POSTed to /api/learnings/<id>/restore", restorePosted != null && restorePosted.includes(ARCH_ID), String(restorePosted));
  check("[c] the mock's row state actually flipped (contract sanity)", rows.find((r) => r.id === ARCH_ID).archived === 0);
  check("[c] after the refetch, the restored card carries no badge (toggle still on)",
    (await page.locator(".memory-card", { hasText: "Archived rule" }).locator(".memory-card-badge").count()) === 0);

  // Now prove it independent of the toggle: uncheck "Show archived" and the
  // restored (now-live) row must still be visible — the default,
  // archived-hidden view.
  await page.locator(".memory-archive-toggle input[type=checkbox]").uncheck();
  await page.waitForTimeout(200);
  check("[c] the restored card is visible in the default (archived-hidden) view",
    await page.locator(".memory-card", { hasText: "Archived rule" }).isVisible().catch(() => false));
  check("[c] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── Scenario (d): Dismiss — client-side only, no network write ──────────────
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const nonGetAfterClick = [];
  let clicked = false;
  page.on("request", (req) => {
    if (clicked && req.method() !== "GET") nonGetAfterClick.push(`${req.method()} ${req.url()}`);
  });
  await installRoutes(page, baseRows());
  await openRules(page);

  await page.locator(".memory-archive-toggle input[type=checkbox]").check();
  await page.waitForTimeout(200);

  const supCard = page.locator(".memory-card", { hasText: "Superseded rule" });
  clicked = true;
  await supCard.getByRole("button", { name: "Dismiss" }).click();
  await page.waitForTimeout(400);

  check("[d] the dismissed card disappears from the DOM", (await page.locator(".memory-card", { hasText: "Superseded rule" }).count()) === 0);
  check("[d] dismiss issued no non-GET request (client-side only)", nonGetAfterClick.length === 0, nonGetAfterClick.join(", "));
  check("[d] the other archived card is unaffected", await page.locator(".memory-card", { hasText: "Archived rule" }).isVisible().catch(() => false));
  check("[d] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
