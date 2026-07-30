// 5D — 3-lane board; Done/Failed as nav rows (1.5: grouped under "Work", above the
// connection indicator) opening a TABLE view. Color still encodes the outcome — as
// a tinted count badge now, not an outlined border (that pill bar was replaced by
// the Claude-app-style grouped sidebar nav in task 1.5).
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
await new Promise((r) => srv.listen(4650, r));

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};

const now = new Date().toISOString();
const mk = (id, status, extra = {}) => ({
  id, title: `Task ${id}`, status, kind: "feature", created_at: now, updated_at: now,
  total_tokens: 100_000, total_cache_creation: 50_000, total_cache_read: 900_000, ...extra,
});
const TASKS = [
  mk("gate1aaaabbbbccccdddd", "awaiting_input", { blocker_question: "Which one?" }),
  mk("work1aaaabbbbccccdddd", "implementing"),
  mk("rev01aaaabbbbccccdddd", "awaiting_approval"),
  // Open-PR (operator demo finding): a DONE ticket must still link to its PR —
  // done1 has a real https PR, done2 the demo DB's local-pr:// (text-only badge).
  // The titles are REAL-length (~90 chars, mean production title is ~80): the
  // badge sat after the title inside a 260px overflow:hidden cell, so a real
  // title clipped it invisible while it stayed in the tab order (review D1) —
  // short fixture titles ("Task done1…") could never reproduce that.
  mk("done1aaaabbbbccccdddd", "done", {
    pr_url: "https://example.com/repo/pull/13",
    title: "done1 — Per-PR CI_GATE integration test pipeline for metrics-core-query-service PR #531 triage",
  }),
  mk("done2aaaabbbbccccdddd", "done", {
    pr_url: "local-pr://tasks/14",
    title: "done2 — Per-PR CI_GATE integration test pipeline for metrics-core-query-service PR #532 triage",
  }),
  mk("fail1aaaabbbbccccdddd", "failed", { cancelled: false }),
  mk("canc1aaaabbbbccccdddd", "failed", { cancelled: true }),
  mk("canc2aaaabbbbccccdddd", "failed", { cancelled: true }),
];

const browser = await chromium.launch();

for (const theme of ["dark", "light"]) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push("PAGEERROR: " + e.message));
  page.on("console", (m) => { if (m.type() === "error" && !m.text().includes("WebSocket")) errors.push(m.text()); });
  await page.addInitScript((t) => localStorage.setItem("nh-theme", t), theme);
  await page.route("**/api/**", (route) => {
    const u = route.request().url();
    const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
    if (route.request().method() !== "GET") return j({});
    if (u.includes("/api/onboarding")) return j({ completed: true });
    if (u.includes("/api/projects")) return j([]);
    if (u.match(/\/api\/tasks\/[^/]+\/events/)) return j([]);
    if (u.match(/\/api\/tasks\/[^/]+\/diff/)) return j({ diff: "" });
    if (u.match(/\/api\/tasks\/[^/]+$/)) return j(TASKS.find((t) => u.includes(t.id)) || TASKS[0]);
    if (u.includes("/api/tasks")) return j(TASKS);
    return j({});
  });
  await page.goto("http://127.0.0.1:4650/", { waitUntil: "networkidle" });
  await page.waitForTimeout(700);

  // R1 — exactly three lanes, and they are the GATE lanes.
  const lanes = await page.locator(".lane-title").allInnerTexts();
  check(`[${theme}] R1 the board shows exactly 3 lanes`, lanes.length === 3, lanes.join(" | "));
  check(`[${theme}] R1 they are Needs Answer / Working / Review PR`,
    JSON.stringify(lanes.map((l) => l.toLowerCase())) === JSON.stringify(["needs answer", "working", "review pr"]),
    lanes.join(" | "));
  const boardText = await page.locator(".nh-board").innerText();
  check(`[${theme}] R1 no Done or Failed column on the board`,
    !/^(done|failed)$/im.test(boardText), boardText.split("\n")[0]);

  // R2 — Done and Failed are nav rows in the sidebar's "Work" group (1.5),
  // above the connection indicator, with a state-colored count badge.
  const doneBtn = page.getByRole("button", { name: /^Done/ });
  const failedBtn = page.getByRole("button", { name: /^Failed/ });
  check(`[${theme}] R2 a Done row and a Failed row exist`,
    (await doneBtn.count()) === 1 && (await failedBtn.count()) === 1);
  const geom = await page.evaluate(() => {
    const rows = [...document.querySelectorAll(".nh-navrow")];
    const done = rows.find((b) => b.textContent.trim().startsWith("Done"));
    const failed = rows.find((b) => b.textContent.trim().startsWith("Failed"));
    const conn = [...document.querySelectorAll(".nh-status-indicator")].pop().getBoundingClientRect();
    // Badge backgrounds are color-mix(in oklab, ...) — getComputedStyle can report
    // those back as an oklab() string rather than rgb(), which a plain \d+ regex
    // mis-parses. Round-trip through a canvas 2D context to normalize to rgb().
    const toRgb = (css) => { const c = document.createElement("canvas").getContext("2d"); c.fillStyle = css; return c.fillStyle; };
    return {
      aboveConnected: done.getBoundingClientRect().bottom <= conn.top + 1
        && failed.getBoundingClientRect().bottom <= conn.top + 1,
      doneBadge: toRgb(getComputedStyle(done.querySelector(".nh-navrow-badge")).backgroundColor),
      failedBadge: toRgb(getComputedStyle(failed.querySelector(".nh-navrow-badge")).backgroundColor),
    };
  });
  check(`[${theme}] R2 the rows sit ABOVE the Connected indicator`, geom.aboveConnected);
  // The badges are color-mix(in oklab, --state-done/--state-failed ...), and this
  // Chromium build's computed-style/canvas serialization keeps oklab() notation
  // rather than folding it to rgb() — a plain \d+ rgb split mis-reads the decimals.
  // Read the OKLab `a` axis directly instead: negative a leans green, positive
  // leans red, which is exactly the green/red state distinction being asserted.
  const oklabA = (c) => { const m = c.match(/oklab\(\s*[\d.]+\s+(-?[\d.]+)/); return m ? Number(m[1]) : NaN; };
  const isGreen = (c) => c.startsWith("rgb") ? (() => { const [r, g, b] = c.match(/\d+/g).map(Number); return g > r && g > b; })() : oklabA(c) < -0.02;
  const isRed   = (c) => c.startsWith("rgb") ? (() => { const [r, g, b] = c.match(/\d+/g).map(Number); return r > g && r > b; })() : oklabA(c) > 0.02;
  check(`[${theme}] R2 Done's count badge is tinted GREEN`, isGreen(geom.doneBadge), geom.doneBadge);
  check(`[${theme}] R2 Failed's count badge is tinted RED`, isRed(geom.failedBadge), geom.failedBadge);

  // Counts: a cancel is NOT a failure (M2), but it is not hidden either.
  const failedLabel = await failedBtn.innerText();
  check(`[${theme}] R2 the Failed count excludes cancels (1 real failure, not 3)`,
    failedLabel.includes("1"), failedLabel.replace(/\n/g, " "));
  check(`[${theme}] R2 the Done count is right`, (await doneBtn.innerText()).includes("2"));

  // R3 — clicking opens a TABLE of those tasks.
  await failedBtn.click();
  await page.waitForTimeout(500);
  const table = page.locator(".stats-table");
  check(`[${theme}] R3 Failed opens a table view`, await table.isVisible());
  const rows = await page.locator(".stats-table tbody tr").count();
  check(`[${theme}] R3 the table lists all 3 failed-lane tasks (incl. cancels)`, rows === 3, `${rows} rows`);
  const sub = await page.locator(".outcome-sub").innerText();
  check(`[${theme}] R3 the summary separates failures from cancels`,
    /1 failure/.test(sub) && /2 cancelled/.test(sub), sub);
  if (theme === "dark") await page.screenshot({ path: `${OUT}/outcomes-failed-dark.png` });

  // A row opens the SAME drawer the board cards open — with the keyboard, too.
  await page.locator(".stats-table tbody tr").first().focus();
  await page.keyboard.press("Enter");
  await page.waitForTimeout(600);
  check(`[${theme}] R3 Enter on a row opens the task drawer (and it stays open)`,
    await page.locator(".slideover").isVisible().catch(() => false));
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);

  await doneBtn.click();
  await page.waitForTimeout(400);
  check(`[${theme}] R3 Done opens its own table`, (await page.locator(".stats-table tbody tr").count()) === 2);

  // Open-PR — a DONE row links straight to its PR without opening the drawer.
  const doneRow = page.locator(".stats-table tbody tr", { hasText: "done1" });
  const prLink = doneRow.locator("a.card-pr-badge");
  check(`[${theme}] a DONE row carries the Open PR anchor with the right href/target`,
    (await prLink.count()) === 1
      && (await prLink.getAttribute("href")) === "https://example.com/repo/pull/13"
      && (await prLink.getAttribute("target")) === "_blank",
    `count=${await prLink.count()}`);
  check(`[${theme}] the anchor is labelled (Open PR), not a cryptic pill`,
    /Open PR/.test((await prLink.textContent().catch(() => "")) || ""));
  // D1 — presence is not enough: the seeded title is REAL-length (~90 chars),
  // and the anchor must have an on-screen box INSIDE its cell and WIN the
  // hit-test at its own center. Under the old 260px ellipsis cell the badge
  // laid out past the clip: focusable, count()===1, and invisible at every
  // viewport (WCAG 2.4.7 focus-visible / 2.4.11 focus-not-obscured).
  // NO scrollIntoView first: an overflow:hidden cell is still programmatically
  // scrollable, so scrolling would "reveal" the clipped anchor and fake a pass —
  // the assertion is about the DEFAULT state a user actually sees.
  const rowHit = await prLink.evaluate((a) => {
    const r = a.getBoundingClientRect();
    const cell = a.closest("td").getBoundingClientRect();
    const el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return { w: Math.round(r.width), h: Math.round(r.height),
      insideCell: r.width > 0 && r.right <= cell.right + 1 && r.left >= cell.left - 1,
      hit: !!el && (el === a || a.contains(el)) };
  });
  check(`[${theme}] the anchor has a real box inside its (overflow:hidden) cell`,
    rowHit.w > 10 && rowHit.h > 8 && rowHit.insideCell, JSON.stringify(rowHit));
  check(`[${theme}] elementFromPoint at the anchor's center resolves to the anchor`,
    rowHit.hit, JSON.stringify(rowHit));
  const localRow = page.locator(".stats-table tbody tr", { hasText: "done2" });
  check(`[${theme}] a non-http pr_url (local-pr://) renders text-only, never a dead link`,
    (await localRow.locator("a.card-pr-badge").count()) === 0
      && (await localRow.locator("span.card-pr-badge").count()) === 1);
  check(`[${theme}] the text-only badge is also visible inside its cell (not clipped)`,
    await localRow.locator("span.card-pr-badge").evaluate((s) => {
      const r = s.getBoundingClientRect();
      const cell = s.closest("td").getBoundingClientRect();
      return r.width > 5 && r.right <= cell.right + 1;
    }).catch(() => false));
  // Block navigation only (capture-phase preventDefault; target="_blank" is
  // asserted above) - propagation is untouched, so the row would still see the
  // click and open the drawer if stopPropagation were missing.
  await page.evaluate(() => {
    document.addEventListener("click", (e) => {
      if (e.target.closest && e.target.closest("a.card-pr-badge")) e.preventDefault();
    }, true);
  });
  await prLink.click();
  await page.waitForTimeout(400);
  check(`[${theme}] clicking Open PR does NOT open the row's drawer`,
    !(await page.locator(".slideover").isVisible().catch(() => false)));
  if (theme === "dark") await page.screenshot({ path: `${OUT}/outcomes-done-dark.png` });

  // Back to the board.
  await page.getByRole("button", { name: /^Board/ }).click();
  await page.waitForTimeout(400);
  check(`[${theme}] back to the board, still 3 lanes`, (await page.locator(".lane-title").count()) === 3);
  if (theme === "dark") await page.screenshot({ path: `${OUT}/board-3lane-dark.png` });
  if (theme === "light") await page.screenshot({ path: `${OUT}/board-3lane-light.png` });

  const overflow = await page.evaluate(() => document.querySelector(".nh-board").scrollWidth > document.querySelector(".nh-board").clientWidth + 1);
  check(`[${theme}] the 3 lanes fit with no horizontal scroll`, !overflow);
  check(`[${theme}] zero page errors`, errors.length === 0, errors[0] || "");
  await ctx.close();
}

await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
