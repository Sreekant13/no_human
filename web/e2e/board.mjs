// Board regression guards (UI_AUDIT B1, B2, M5, M6, M10) — driven against the BUILT bundle
// with a seeded lane that overflows, so the squash is reproducible and deterministic.
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
await new Promise((r) => srv.listen(4630, r));

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};

const now = new Date().toISOString();
// 9 parked tasks: enough to overflow the Needs-Answer lane, which NEVER collapses.
const answer = Array.from({ length: 9 }, (_, i) => ({
  id: `park${i}aaaabbbbccccdddd`,
  title: `Per-PR CI_GATE Integration Test Pipeline ${i}`,
  description_short:
    "Fetch all code review comments on https://code.example.com/dev/metrics-core-query-service/pull/531 and triage them",
  status: "awaiting_input",
  kind: "feature",
  blocker_question: "The PR was closed without merging. Abandon the task, or rework and reopen?",
  attempts: 3,
  total_tokens: 2_390_000,
  created_at: now,
  updated_at: now,
}));
const failedCancelled = Array.from({ length: 10 }, (_, i) => ({
  id: `canc${i}aaaabbbbccccdddd`,
  title: `Cancelled thing ${i}`,
  status: "failed",
  cancelled: true,
  kind: "feature",
  created_at: now,
  updated_at: now,
}));
const older = new Date(Date.now() - 3600e3).toISOString();
const failedReal = [
  { id: "realfailaaaabbbbcccc", title: "Per-PR CI_GATE Pipeline", status: "failed", cancelled: false,
    kind: "bugfix", created_at: older, updated_at: older },
  // A cancel that is NEWER and shares the title: it used to head the group and hide the failure.
  { id: "cancelnewaaaabbbbcc", title: "Per-PR CI_GATE Pipeline", status: "failed", cancelled: true,
    kind: "bugfix", created_at: now, updated_at: now },
];
const TASKS = [...answer, ...failedCancelled, ...failedReal];

const browser = await chromium.launch();

for (const theme of ["dark", "light"]) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("console", (m) => { if (m.type() === "error" && !m.text().includes("WebSocket")) errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  await page.addInitScript((t) => localStorage.setItem("nh-theme", t), theme);
  await page.route("**/api/**", (route) => {
    const u = route.request().url();
    const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
    if (u.includes("/api/onboarding")) return j({ completed: true });
    if (u.includes("/api/tasks")) return j(TASKS);
    if (u.includes("/api/projects")) return j([]);
    return j({});
  });
  await page.goto("http://127.0.0.1:4630/", { waitUntil: "networkidle" });
  await page.waitForTimeout(700);

  // B1 — a full lane must SCROLL, not squash its cards.
  const squash = await page.evaluate(() => {
    const cards = [...document.querySelectorAll(".task-card")];
    const bad = cards
      .map((c) => ({ h: Math.round(c.getBoundingClientRect().height), need: c.scrollHeight, id: c.textContent.slice(0, 12) }))
      .filter((c) => c.h < c.need - 1); // rendered shorter than its own content
    const lane = document.querySelector(".lane-answer .lane-body");
    return { bad, laneScrolls: lane ? lane.scrollHeight > lane.clientHeight : false };
  });
  check(`[${theme}] B1 no card is squashed below its content height`, squash.bad.length === 0,
    squash.bad.length ? JSON.stringify(squash.bad.slice(0, 3)) : "");
  check(`[${theme}] B1 the overflowing lane scrolls instead`, squash.laneScrolls);

  // M5 — long unbreakable tokens (a URL) must not be sliced mid-glyph.
  const clipped = await page.evaluate(() => {
    const sel = [".card-title", ".card-description", ".card-blocker-q"];
    const bad = [];
    for (const s of sel) {
      for (const el of document.querySelectorAll(s)) {
        if (el.scrollWidth > el.clientWidth + 1) bad.push({ s, sw: el.scrollWidth, cw: el.clientWidth });
      }
    }
    return bad;
  });
  check(`[${theme}] M5 no card text overflows its box horizontally`, clipped.length === 0,
    clipped.length ? JSON.stringify(clipped.slice(0, 2)) : "");

  // M2 — a cancelled task must not be counted as a failure. SCRUM-84 moved this
  // all-time failed/cancelled tally off the always-on overview strip; it now
  // lives only in the Failed nav row's tooltip (App.jsx `title={...}`).
  const failedTitle = await page.locator("button.nh-navrow", { hasText: "Failed" }).getAttribute("title");
  check(`[${theme}] M2 cancelled tasks are not counted as failures`,
    /\b1 failed\b/.test(failedTitle) && /11 cancelled/.test(failedTitle), failedTitle);

  // (M2b and the failed-lane pill checks moved to drive-5d.mjs — 5D took Failed off the board.)

  // The blocker question must not bleed a sliced line through its bottom padding.
  const qClip = await page.evaluate(() => {
    const el = document.querySelector(".card-blocker-q");
    const inner = el.querySelector("span");
    return { outer: el.scrollHeight <= el.clientHeight + 1, hasInner: Boolean(inner) };
  });
  check(`[${theme}] blocker question clamps cleanly (no sliced sliver)`, qClip.outer && qClip.hasInner);

  // M6 — every lane, including DONE, fits the 1440px laptop.
  const lanes = await page.evaluate(() => {
    const board = document.querySelector(".nh-board");
    return { scrollW: board.scrollWidth, clientW: board.clientWidth };
  });
  check(`[${theme}] M6 the board fits a 1440px laptop (no hidden DONE lane)`,
    lanes.scrollW <= lanes.clientW + 1, `scrollWidth ${lanes.scrollW} vs ${lanes.clientW}`);

  // M10 — the lane container must be visible in BOTH themes.
  const laneBg = await page.evaluate(() => {
    const lane = document.querySelector(".lane");
    const board = document.querySelector(".nh-board");
    const parse = (c) => (c.match(/[\d.]+/g) || []).map(Number);
    return { lane: getComputedStyle(lane).backgroundColor, board: getComputedStyle(board).backgroundColor,
             laneRgb: parse(getComputedStyle(lane).backgroundColor) };
  });
  // A lane painted in white-alpha is invisible on a light canvas: require a non-white-alpha fill.
  const isWhiteAlpha = laneBg.laneRgb.length === 4 && laneBg.laneRgb[0] === 255 && laneBg.laneRgb[1] === 255 && laneBg.laneRgb[2] === 255;
  check(`[${theme}] M10 lane surface is not a white-alpha literal`, !isWhiteAlpha, laneBg.lane);

  // B2 — Enter on a focused card must OPEN the drawer and leave it open.
  await page.locator(".task-card").first().focus();
  await page.keyboard.press("Enter");
  await page.waitForTimeout(500);
  const drawerOpen = await page.locator(".slideover").isVisible().catch(() => false);
  check(`[${theme}] B2 Enter opens the task drawer (and it stays open)`, drawerOpen);
  if (drawerOpen) {
    await page.keyboard.press("Escape");
    await page.waitForTimeout(300);
  }

  if (theme === "dark") await page.screenshot({ path: `${OUT}/board-fixed-dark.png` });
  if (theme === "light") await page.screenshot({ path: `${OUT}/board-fixed-light.png` });
  check(`[${theme}] zero console errors`, errors.length === 0, errors[0] || "");
  await ctx.close();
}

await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
