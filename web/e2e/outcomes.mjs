// 5D — 3-lane board; Done/Failed as outlined buttons above "Connected" opening a TABLE view.
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
  mk("done1aaaabbbbccccdddd", "done"),
  mk("done2aaaabbbbccccdddd", "done"),
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

  // R2 — two outlined buttons, above the connection indicator.
  const doneBtn = page.locator(".nh-outcome-done");
  const failedBtn = page.locator(".nh-outcome-failed");
  check(`[${theme}] R2 a Done button and a Failed button exist`,
    (await doneBtn.count()) === 1 && (await failedBtn.count()) === 1);
  const geom = await page.evaluate(() => {
    const outcomes = document.querySelector(".nh-outcomes").getBoundingClientRect();
    const conn = [...document.querySelectorAll(".nh-status-indicator")].pop().getBoundingClientRect();
    const rgb = (css) => { const c = document.createElement("canvas").getContext("2d"); c.fillStyle = css; return c.fillStyle; };
    return {
      aboveConnected: outcomes.bottom <= conn.top + 1,
      doneBorder: getComputedStyle(document.querySelector(".nh-outcome-done")).borderTopColor,
      failedBorder: getComputedStyle(document.querySelector(".nh-outcome-failed")).borderTopColor,
      doneFill: getComputedStyle(document.querySelector(".nh-outcome-done")).backgroundColor,
    };
  });
  check(`[${theme}] R2 the buttons sit ABOVE the Connected indicator`, geom.aboveConnected);
  const isGreen = (c) => { const [r, g, b] = c.match(/\d+/g).map(Number); return g > r && g > b; };
  const isRed = (c) => { const [r, g, b] = c.match(/\d+/g).map(Number); return r > g && r > b; };
  check(`[${theme}] R2 Done is outlined GREEN`, isGreen(geom.doneBorder), geom.doneBorder);
  check(`[${theme}] R2 Failed is outlined RED`, isRed(geom.failedBorder), geom.failedBorder);
  check(`[${theme}] R2 outlined, not filled`, geom.doneFill === "rgba(0, 0, 0, 0)", geom.doneFill);

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
