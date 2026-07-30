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
// Open-PR affordance (operator demo finding): a card whose payload carries a
// pr_url must link straight to the PR. The demo DB's local-pr:// URLs must
// degrade to a text badge, never a dead link. (DONE tasks left the board in 5D;
// their Open PR link lives in the Outcomes table - covered by e2e/outcomes.mjs.)
// Titles are REAL-length (~90 chars; production mean is ~80): a short fixture
// title let a clipped-invisible badge pass the presence checks (review D1).
const prTasks = [
  { id: "prawaitaaaabbbbcccc1",
    title: "Awaiting task with PR — per-PR CI_GATE integration test pipeline for metrics-core-query #12",
    status: "awaiting_approval",
    kind: "feature", pr_url: "https://example.com/repo/pull/12", created_at: now, updated_at: now },
  { id: "prlocalaaaabbbbccc33",
    title: "Awaiting task with local PR — per-PR CI_GATE integration pipeline for metrics-core-qs #14",
    status: "awaiting_approval",
    kind: "feature", pr_url: "local-pr://tasks/14", created_at: now, updated_at: now },
];
const TASKS = [...answer, ...failedCancelled, ...failedReal, ...prTasks];

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

  // Open-PR — the card links straight to the PR, without opening the drawer.
  const prCard = page.locator(".task-card", { hasText: "Awaiting task with PR" }).first();
  const prLink = prCard.locator("a.card-pr-badge");
  check(`[${theme}] the card carries the Open PR anchor with the right href/target`,
    await prLink.count() === 1
      && (await prLink.getAttribute("href")) === "https://example.com/repo/pull/12"
      && (await prLink.getAttribute("target")) === "_blank",
    `count=${await prLink.count()}`);
  check(`[${theme}] the anchor is labelled (Open PR), not a cryptic pill`,
    /Open PR/.test((await prLink.textContent().catch(() => "")) || ""));
  check(`[${theme}] the anchor is keyboard-reachable (tabbable)`,
    await prLink.evaluate((el) => el.tabIndex >= 0).catch(() => false));
  // D1 — presence is not enough: with a REAL-length title the anchor must have
  // an on-screen box and WIN the hit-test at its own center. A clipped anchor
  // stays in the tab order but can never be seen or clicked (WCAG 2.4.7/2.4.11).
  // No scrollIntoView first: it can scroll an overflow:hidden ancestor and
  // "reveal" a clipped badge — measure the DEFAULT state the user sees.
  const cardHit = await prLink.evaluate((a) => {
    const r = a.getBoundingClientRect();
    const el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return { w: Math.round(r.width), h: Math.round(r.height),
      hit: !!el && (el === a || a.contains(el)) };
  });
  check(`[${theme}] the anchor has a real on-screen box (not clipped away)`,
    cardHit.w > 10 && cardHit.h > 8, JSON.stringify(cardHit));
  check(`[${theme}] elementFromPoint at the anchor's center resolves to the anchor`,
    cardHit.hit, JSON.stringify(cardHit));
  const localCard = page.locator(".task-card", { hasText: "Awaiting task with local PR" });
  check(`[${theme}] non-http pr_url (local-pr://) renders text-only, never a dead link`,
    (await localCard.locator("a.card-pr-badge").count()) === 0
      && (await localCard.locator("span.card-pr-badge").count()) === 1);
  // Clicking the anchor must NOT open the drawer. Block navigation via a
  // capture-phase preventDefault (the target="_blank" contract is asserted
  // above) - propagation is left alone so the card would still see the click
  // if stopPropagation were missing.
  await page.evaluate(() => {
    document.addEventListener("click", (e) => {
      if (e.target.closest && e.target.closest("a.card-pr-badge")) e.preventDefault();
    }, true);
  });
  await prLink.click();
  await page.waitForTimeout(400);
  const drawerAfterPr = await page.locator(".slideover").isVisible().catch(() => false);
  check(`[${theme}] clicking Open PR does NOT open the drawer`, !drawerAfterPr);

  if (theme === "dark") await page.screenshot({ path: `${OUT}/board-fixed-dark.png` });
  if (theme === "light") await page.screenshot({ path: `${OUT}/board-fixed-light.png` });
  check(`[${theme}] zero console errors`, errors.length === 0, errors[0] || "");
  await ctx.close();
}

await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
