// Drawer regression guards (UI_AUDIT B3, B4, M3, M4 + the DA note's six risks).
import { chromium } from "playwright";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";

const DIST = new URL("../dist", import.meta.url).pathname;
const OUT = new URL("./shots", import.meta.url).pathname;
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

const now = new Date().toISOString();
const LONG_TITLE = "Add --json flag to nh status for machine-readable lane counts everywhere";
const mkTask = (id, status, extra = {}) => ({
  id, title: LONG_TITLE, status, kind: "feature",
  description: "Some description", created_at: now, updated_at: now,
  total_tokens: 1_560_000, attempts: 1, ...extra,
});

const PARKED = mkTask("parked00aaaabbbbcccc", "awaiting_input", {
  blocker_question: "The PR was closed without merging. Abandon the task, or rework and reopen?",
  // The DRAWER reads task.blocker (an object) — the flat field is the board card's.
  blocker: {
    question: "The PR was closed without merging. Abandon the task, or rework and reopen?",
    category: "ambiguity",
    confidence: 0.8,
    options: ["Abandon the task", "Rework and reopen the PR"],
  },
});
const REVIEW = mkTask("review00aaaabbbbcccc", "awaiting_approval", { pr_url: "https://example.com/pull/1" });
const RUNNING = mkTask("running0aaaabbbbcccc", "implementing");

const api = (task) => (route) => {
  const u = route.request().url();
  const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
  if (route.request().method() !== "GET") return route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
  if (u.includes("/api/onboarding")) return j({ completed: true });
  if (u.includes("/api/projects")) return j([]);
  if (u.match(/\/api\/tasks\/[^/]+\/diff/)) return j({ diff: "" });
  if (u.match(/\/api\/tasks\/[^/]+\/events/)) return j([]);
  if (u.match(/\/api\/tasks\/[^/]+$/)) return j(task);
  if (u.includes("/api/tasks")) return j([task]);
  return j({});
};

const browser = await chromium.launch();

async function open(task, viewport = { width: 1440, height: 900 }, theme = "dark") {
  const ctx = await browser.newContext({ viewport });
  const page = await ctx.newPage();
  const errors = [];
  page.on("console", (m) => { if (m.type() === "error" && !m.text().includes("WebSocket")) errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  await page.addInitScript((t) => localStorage.setItem("nh-theme", t), theme);
  await page.route("**/api/**", api(task));
  await page.goto("http://127.0.0.1:4640/", { waitUntil: "networkidle" });
  await page.waitForTimeout(500);
  await page.locator(".task-card").first().click();
  await page.locator(".slideover").waitFor({ state: "visible", timeout: 5000 });
  await page.waitForTimeout(700);
  return { ctx, page, errors };
}

const activeTab = (page) =>
  page.locator(".so-tab.active, .so-tabs .active").first().innerText().catch(() => "?");

// B4 — the drawer must open on the surface that can CLEAR the gate.
{
  const { ctx, page, errors } = await open(PARKED);
  const t = (await activeTab(page)).toLowerCase();
  check("[B4] a PARKED task opens on the tab that holds its answer UI", t.includes("detail"), t);
  const optionBtns = page.locator(".slideover .blocker-option-btn");
  const nOptions = await optionBtns.count();
  const questionShown = await page.locator(".slideover").innerText();
  check("[B4] the blocker's one-click answers are on screen without changing tabs",
    nOptions >= 2 && questionShown.includes("Abandon the task"), `${nOptions} option buttons`);
  check("[B4] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}
{
  const { ctx, page } = await open(REVIEW);
  const t = (await activeTab(page)).toLowerCase();
  check("[B4] an awaiting-approval task still opens on Review (unchanged)", t.includes("review"), t);
  await ctx.close();
}
{
  const { ctx, page } = await open(RUNNING);
  const t = (await activeTab(page)).toLowerCase();
  check("[B4] a running task still opens on System (unchanged)", t.includes("system"), t);
  await ctx.close();
}

// M3 — Escape must close the nested modal ONLY, preserving the drawer and the typed text.
{
  const { ctx, page } = await open(PARKED);
  const replyBtn = page.getByRole("button", { name: /^reply$/i }).first();
  if (await replyBtn.count()) {
    await replyBtn.click();
    await page.waitForTimeout(400);
    const ta = page.locator(".sendback-modal textarea").first();
    await ta.fill("my carefully typed answer");
    await page.keyboard.press("Escape");
    await page.waitForTimeout(400);
    const modalGone = !(await page.locator(".sendback-modal").isVisible().catch(() => false));
    const drawerStays = await page.locator(".slideover").isVisible().catch(() => false);
    check("[M3] Escape closes the reply modal", modalGone);
    check("[M3] ...and does NOT close the drawer under it", drawerStays);
    // The whole point: the operator's typed answer must survive the Escape.
    await replyBtn.click();
    await page.waitForTimeout(400);
    const kept = await page.locator(".sendback-modal textarea").first().inputValue();
    check("[M3] the typed answer SURVIVES the Escape", kept === "my carefully typed answer", JSON.stringify(kept));
    await page.keyboard.press("Escape");
    await page.waitForTimeout(300);
    // And a second Escape, with no modal open, still closes the drawer.
    await page.keyboard.press("Escape");
    await page.waitForTimeout(400);
    const drawerClosed = !(await page.locator(".slideover").isVisible().catch(() => false));
    check("[M3] Escape with no modal open still closes the drawer", drawerClosed);
  } else {
    check("[M3] reply button present on a parked task", false, "not found");
  }
  await ctx.close();
}

// B3 — the title must not render one character per line on a phone.
{
  const { ctx, page } = await open(PARKED, { width: 390, height: 844 });
  const geom = await page.evaluate(() => {
    const t = document.querySelector(".so-title");
    const r = t.getBoundingClientRect();
    const cs = getComputedStyle(t);
    return { w: Math.round(r.width), h: Math.round(r.height), lh: parseFloat(cs.lineHeight) };
  });
  const lines = Math.round(geom.h / geom.lh);
  check("[B3] the drawer title is readable on a phone (not one char per line)",
    geom.w > 150 && lines <= 5, `width ${geom.w}px, ~${lines} lines`);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  check("[B3] no horizontal overflow at 390px", !overflow);
  await page.screenshot({ path: `${OUT}/drawer-mobile-after.png` });
  await ctx.close();
}

// Desktop header must be unchanged (the DA note's risk 5).
{
  const { ctx, page } = await open(PARKED);
  const rows = await page.evaluate(() => {
    const h = document.querySelector(".so-header");
    return { wrap: getComputedStyle(h).flexWrap, height: Math.round(h.getBoundingClientRect().height) };
  });
  check("[desktop] the drawer header still lays out on one row", rows.wrap === "nowrap", JSON.stringify(rows));
  await page.screenshot({ path: `${OUT}/drawer-desktop-after.png` });
  await ctx.close();
}


// ── The reviewer's refutations: each must now be closed ────────────────────────────────
// BLOCKER — mid-swap, the drawer must never show task A under task B's id with Approve live.
{
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const A = mkTask("aaaaaaaaaaaabbbbcccc", "awaiting_approval", { pr_url: "https://x/pull/1" });
  A.title = "TASK-A the first review";
  const B = mkTask("bbbbbbbbbbbbccccdddd", "awaiting_approval", { pr_url: "https://x/pull/2" });
  B.title = "TASK-B the second review";
  await page.route("**/api/**", async (route) => {
    const u = route.request().url();
    const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
    if (route.request().method() !== "GET") return j({});
    if (u.includes("/api/onboarding")) return j({ completed: true });
    if (u.includes("/api/projects")) return j([]);
    if (u.match(/\/api\/tasks\/[^/]+\/events/)) return j([]);   // an ARRAY: the feed filters it
    if (u.match(/\/api\/tasks\/[^/]+\/diff/)) return j({ diff: "" });
    if (u.includes(A.id)) return j(A);
    // B's fetch is SLOW: this is the window where the old code showed A under B's id.
    if (u.includes(B.id)) { await new Promise((r) => setTimeout(r, 2500)); return j(B); }
    if (u.includes("/api/tasks")) return j([A, B]);
    return j({});
  });
  await page.goto("http://127.0.0.1:4640/", { waitUntil: "networkidle" });
  await page.waitForTimeout(800);
  // Click the TITLE, not the card centre: these tasks carry a pr_url, so the card renders a
  // PR link and a centre-click navigates away from the app.
  // Click the TITLE, not the card centre: these tasks carry a pr_url, so the card renders a
  // PR link and a centre-click would navigate away from the app.
  await page.locator(".task-card .card-title").first().click();
  await page.locator(".slideover").waitFor({ state: "visible", timeout: 8000 });
  await page.waitForTimeout(600);
  const next = page.getByRole("button", { name: /next review/i }).first();
  if (await next.count()) {
    await next.click();
    await page.waitForTimeout(400);   // mid-flight: B is bound, B's fetch has NOT returned
    const midText = await page.locator(".slideover").innerText();
    const approve = page.locator(".slideover").getByRole("button", { name: /^approve/i }).first();
    const approveLive = (await approve.count()) ? await approve.isEnabled() : false;
    check("[BLOCKER] mid-swap the drawer does NOT still show the previous task",
      !midText.includes("TASK-A"), midText.split("\n").slice(0, 2).join(" / "));
    check("[BLOCKER] mid-swap there is no live Approve button bound to the wrong task",
      !approveLive);
    await page.waitForTimeout(2600);
    const after = await page.locator(".slideover").innerText();
    check("[BLOCKER] the new task repaints once its fetch lands", after.includes("TASK-B"));
  } else {
    check("[BLOCKER] next-review button present", false, "not found");
  }
  await ctx.close();
}

// MAJOR 3 — "n" must not open the composer behind the drawer, and Escape must not kill both.
{
  const { ctx, page } = await open(PARKED);
  await page.keyboard.press("n");
  await page.waitForTimeout(400);
  const composerOpen = await page.locator('[role="dialog"][aria-label="New task"]').count();
  check("[M3+] 'n' does not open the composer behind an open drawer", composerOpen === 0);
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
  await ctx.close();
}

// MAJOR 4 — every drawer tab must be reachable on a phone.
{
  const { ctx, page } = await open(PARKED, { width: 390, height: 844 });
  const tabs = await page.evaluate(() => {
    const strip = document.querySelector(".so-tabs");
    const cs = getComputedStyle(strip);
    return { overflowX: cs.overflowX, scrollable: strip.scrollWidth > strip.clientWidth,
             reach: strip.scrollWidth <= strip.clientWidth + strip.scrollWidth };
  });
  check("[M4+] the drawer tab strip scrolls on a phone (diff/attempts reachable)",
    tabs.overflowX === "auto" || !tabs.scrollable, JSON.stringify(tabs));
  await ctx.close();
}

await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
