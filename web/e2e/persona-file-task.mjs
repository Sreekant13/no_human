// Files a task through the REAL board GUI at :8420, the way a human does — click
// "+ New Task", type into the composer, pick Kind, click Next, read the intake
// grill, click CREATE TASK — and verifies the row exists afterwards.
//
// WHY THIS EXISTS. The persona walk requires filing through the GUI, not the API.
// When the Chrome extension dropped mid-session I fell back to POST /api/tasks and
// called the GUI half "incomplete" — with Playwright, which drives this same board
// in eleven other suites, sitting right there. Environment excuses are not a
// method. This file removes the excuse permanently.
//
// Usage:  node e2e/persona-file-task.mjs <path-to-json>
//   { "prompt": "first line is the title\n\nbody…", "kind": "Bug fix",
//     "priority": "high" }
//
// It drives the LIVE server (not dist), because that is the surface a user touches
// and the only one where the task actually lands. It is therefore NOT read-only —
// it creates a task. Nothing else is clicked.
import { chromium } from "playwright";
import fs from "node:fs";

const BASE = process.env.NH_BASE || "http://127.0.0.1:8420";
const spec = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const OUT = new URL("./shots", import.meta.url).pathname;
fs.mkdirSync(OUT, { recursive: true });

const errors = [];
const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1512, height: 950 } });
const page = await ctx.newPage();
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));

await page.goto(BASE, { waitUntil: "networkidle" });
await page.waitForTimeout(800);

await page.click("text=+ NEW TASK");
await page.waitForTimeout(600);

// Kind BEFORE typing: the chips are radios, and choosing one must not clear the
// prompt. Selecting it first also proves the post-fix layout — Kind is reachable
// without scrolling past the submit button.
if (spec.kind) {
  await page.click(`[role="radio"]:has-text("${spec.kind}")`);
  await page.waitForTimeout(200);
}

await page.click("form textarea");
await page.fill("form textarea", spec.prompt);
await page.waitForTimeout(300);
await page.screenshot({ path: `${OUT}/persona-composer.png`, fullPage: true });

// Next → runs the intake grill, which can take a while (it explores the repo).
await page.click('form button[type="submit"]');
console.log("clicked Next → ; waiting for the intake grill…");

// CREATE TASK is the real commit point. Waiting for it by NAME is the whole
// point: a previous session clicked Next, watched the grill finish, and told the
// operator the task was "filed" while this button sat unclicked.
const create = page.locator('button:has-text("CREATE TASK"), button:has-text("Create task")');
await create.first().waitFor({ state: "visible", timeout: 600000 });
await page.screenshot({ path: `${OUT}/persona-grill.png`, fullPage: true });

await create.first().click();
await page.waitForTimeout(2500);
await page.screenshot({ path: `${OUT}/persona-board.png`, fullPage: true });

// Verify against the system of record, not against the screen. "Filed" is not
// utterable until a query returns the row.
const title = spec.prompt.split("\n")[0].trim().slice(0, 40);
const res = await page.evaluate(async (b) => (await fetch(`${b}/api/tasks`)).json(), BASE);
const hit = (Array.isArray(res) ? res : []).find((t) => (t.title || "").includes(title.slice(0, 25)));

await browser.close();

if (errors.length) { console.error("PAGE ERRORS:\n" + errors.join("\n")); process.exit(1); }
if (!hit) { console.error(`NOT FILED — no task whose title contains: ${title.slice(0, 25)}`); process.exit(1); }
console.log(`FILED ${hit.id.slice(0, 8)}  status=${hit.status}  kind=${hit.kind}`);
console.log(`  ${hit.title}`);
