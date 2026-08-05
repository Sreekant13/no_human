// The multi-ticket queue, driven in a real browser against the BUILT bundle.
//
// WHY A BROWSER TEST AND NOT A UNIT TEST. backlogSelection.test.mjs proves the
// queue's algebra; this proves the WIRING, which is where the defect lived. The
// advance/dequeue logic sat inline in App.jsx and inverting one boolean turned
// "start 10 tickets" into "start 1, silently drop 9" with all 875 unit tests
// still green — no assertion anywhere touched the handler that does the
// advancing. Source-text regex over App.jsx is not a substitute (this repo has
// already been burned by one: sidebarNav.test.mjs:23 is satisfied by unrelated
// text elsewhere in the same file). So: click Start on real checkboxes, and
// count the tickets that actually arrive.
//
// It covers the two bugs together, because they are the same handler:
//   * every picked ticket reaches the intake flow (none silently dropped)
//   * cancelling ONE ticket does not discard the ones behind it
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
await new Promise((r) => srv.listen(4622, r));

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};

// The queue readout, or an explicit "it is gone" — never a 30s hang. A queue
// that was wrongly discarded must read as a clean FAIL line with the reason on
// it, not as a timeout somewhere in the middle of the suite.
const noticeText = async (page) => {
  try { return (await page.locator(".queue-notice-text").first().textContent({ timeout: 3000 })).trim(); }
  catch { return "(no queue notice — the run is no longer open)"; }
};

const ISSUES = [1, 2, 3, 4].map((n) => ({
  tracker: "jira",
  key: `NO-${n}`,
  summary: `Ticket number ${n}`,
  status: "Todo",
  assignee: null,
  // Descending, so the merged order in the two-tracker scenario is predictable.
  updated: `2026-08-0${5 - n}T10:00:00.000+0000`,
  url: `https://acme.atlassian.net/browse/NO-${n}`,
  description: `body of NO-${n}`,
  imported: null,
}));

// A Linear ticket that SHARES a key with a Jira one (NO-1). Both trackers mint
// keys of this shape, and the page must treat them as two different tickets.
const LINEAR_ISSUES = [
  { tracker: "linear", key: "NO-1", summary: "The Linear namesake", status: "Todo",
    assignee: null, updated: "2026-08-04T12:00:00.000Z",
    url: "https://linear.app/acme/issue/NO-1", description: "linear body", imported: null },
  { tracker: "linear", key: "LIN-7", summary: "Only on Linear", status: "Backlog",
    assignee: null, updated: "2026-08-02T12:00:00.000Z",
    url: "https://linear.app/acme/issue/LIN-7", description: "linear body", imported: null },
];

const rowsFor = (tracker) => (tracker === "linear" ? LINEAR_ISSUES : ISSUES);

/**
 * Stub every endpoint the Backlog -> composer -> grill -> create path touches.
 * `opts.hangDetailFor` leaves the single-issue detail GET pending forever, which
 * is the state the "Reading NO-1 from Jira…" overlay renders — up to 30s of it.
 */
async function stubApi(page, state, opts = {}) {
  const configured = opts.trackers || ["jira"];
  await page.route("**/api/**", async (route) => {
    const req = route.request();
    const u = req.url();
    const json = (b, status = 200) =>
      route.fulfill({ status, contentType: "application/json", body: JSON.stringify(b) });

    // Order matters: the issue routes are nested under /api/integrations. Both
    // trackers are stubbed so the page's fan-out is exercised for real — a
    // Linear row that never reaches the browser proves nothing about the wiring.
    const detail = u.match(/\/api\/integrations\/(jira|linear)\/issues\/([^/?]+)/);
    if (detail) {
      const [, tracker, raw] = detail;
      const key = decodeURIComponent(raw);
      if (opts.hangDetailFor === key) return; // never fulfilled — the slow-tracker case
      state.detailCalls.push(`${tracker}:${key}`);
      const issue = rowsFor(tracker).find((i) => i.key === key);
      if (!issue) return json({ detail: `${key} not found` }, 404);
      return json({ ...issue, description: `FULL body of ${key}` });
    }
    const list = u.match(/\/api\/integrations\/(jira|linear)\/issues/);
    if (list) {
      if (opts.failTracker === list[1]) {
        return json({ detail: `${list[1]} is having a bad day.` }, 502);
      }
      return json(rowsFor(list[1]));
    }
    if (u.includes("/api/integrations")) {
      return json({ integrations: [
        { name: "jira", configured: configured.includes("jira") },
        { name: "linear", configured: configured.includes("linear") },
      ] });
    }

    if (u.includes("/api/grill/stream")) {
      // One frame, straight to the refined spec — the five questions have their
      // own suite (grill-a11y.mjs); what matters here is reaching the create.
      state.grills += 1;
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body:
          `data: ${JSON.stringify({
            kind: "grill_result", type: "done",
            title: state.lastGrillTitle = (req.postDataJSON()?.title || ""),
            description: "refined", acceptance_criteria: ["it works"],
          })}\n\n`,
      });
    }
    if (u.includes("/api/tasks") && req.method() === "POST") {
      state.created.push(req.postDataJSON());
      return json({ id: `task-${state.created.length}` }, 201);
    }
    if (u.includes("/api/tasks")) return json([]);
    if (u.includes("/api/projects")) return json([{ id: "p1", name: "no_human", repo_paths: ["/a/x"], primary_repo: "/a/x" }]);
    if (u.includes("/api/repos/discover")) return json({ repos: [], roots_scanned: [], roots_missing: [], roots_refused: [], total_found: 0 });
    if (u.includes("/api/onboarding")) return json({ completed: true });
    return json({});
  });
}

const browser = await chromium.launch();

// ── Scenario A: four tickets — create, cancel, stop ───────────────────────
{
  const state = { created: [], grills: 0, detailCalls: [] };
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await stubApi(page, state);
  await page.goto("http://127.0.0.1:4622/", { waitUntil: "networkidle" });

  await page.locator(".nh-sidenav button", { hasText: "Backlog" }).click();
  await page.waitForTimeout(400);

  const rows = await page.locator('input[type="checkbox"]').count();
  check("the backlog lists every open ticket", rows === 4, `${rows} rows`);

  await page.getByRole("button", { name: "Select all" }).click();
  const startLabel = await page.locator("button", { hasText: /^Start \d+ tasks?$/ }).first().textContent();
  check("the Start button names the count it will start", startLabel.trim() === "Start 4 tasks", startLabel);

  // The warning shown BEFORE the first question must describe what cancelling
  // actually does. It used to promise "Cancelling stops the rest", which was
  // the bug written down as a feature.
  const preNotice = await page.locator("span", { hasText: /one at a time/ }).first().textContent();
  check(
    "the pre-start notice does not promise that cancelling one ticket stops the run",
    /keeps the rest/i.test(preNotice) && !/stops the rest/i.test(preNotice),
    preNotice.trim(),
  );

  await page.locator("button", { hasText: /^Start \d+ tasks?$/ }).first().click();
  await page.waitForTimeout(600);

  // ── ticket 1: create it ────────────────────────────────────────────────
  const n1 = await noticeText(page);
  check("ticket 1 of 4 is announced in the composer", n1 === "Ticket 1 of 4 · NO-1", n1);
  const prompt1 = await page.locator("textarea").first().inputValue();
  check("the composer is prefilled from the ticket's FULL body", prompt1.includes("FULL body of NO-1"), prompt1.slice(0, 40));

  await page.locator("button", { hasText: /^Next/ }).click();
  await page.waitForTimeout(500);
  // P2-8: the readout must survive INTO the intake flow. It used to be handed
  // to the composer alone, so it vanished for the whole grill.
  const nGrill = await noticeText(page);
  check(
    "the queue position is still shown during the intake flow, not only in the composer",
    nGrill === "Ticket 1 of 4 · NO-1",
    nGrill || "(no queue notice rendered on the grill step)",
  );
  await page.getByRole("button", { name: "Create Task" }).click();
  await page.waitForTimeout(700);

  // ── ticket 2: cancel it with Escape (the P0-3 defect) ──────────────────
  const n2 = await noticeText(page);
  check("creating ticket 1 advances to ticket 2", n2 === "Ticket 2 of 4 · NO-2", n2);

  await page.keyboard.press("Escape");
  await page.waitForTimeout(600);
  const n3 = await noticeText(page);
  check(
    "Escape on ticket 2 of 4 cancels THAT ticket and keeps the two behind it",
    n3 === "Ticket 3 of 4 · NO-3",
    n3,
  );

  // ── ticket 3: abandon the run explicitly ───────────────────────────────
  const stop = page.locator(".queue-notice-stop");
  const stopText = await stop.textContent({ timeout: 3000 }).catch(() => "(no stop button)");
  check("the explicit way out names how many tickets it discards", stopText.trim() === "Stop the rest (1)", stopText);
  await stop.click({ timeout: 3000 }).catch(() => {});
  await page.waitForTimeout(500);
  // `[data-nested-modal]` is the composer's root; `.queue-notice` is the queue
  // readout every step of the flow carries. Both gone = the run is over and the
  // operator is back on the Backlog page, not staring at ticket 4.
  const openSteps = await page.locator("[data-nested-modal], .new-task-modal").count();
  const notices = await page.locator(".queue-notice").count();
  check("Stop the rest ends the run", openSteps === 0 && notices === 0,
    `${openSteps} intake steps / ${notices} queue notices still open`);

  check("exactly one task was created — the other three were not started", state.created.length === 1, JSON.stringify(state.created.map((t) => t.external_id)));
  check("the created task carries the ticket it came from", state.created[0]?.external_id === "NO-1" && state.created[0]?.source === "jira", JSON.stringify(state.created[0]));
  check("no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── Scenario B: every picked ticket is walked, none silently dropped ──────
{
  const state = { created: [], grills: 0, detailCalls: [] };
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await stubApi(page, state);
  await page.goto("http://127.0.0.1:4622/", { waitUntil: "networkidle" });
  await page.locator(".nh-sidenav button", { hasText: "Backlog" }).click();
  await page.waitForTimeout(400);
  await page.getByRole("button", { name: "Select all" }).click();
  await page.locator("button", { hasText: /^Start \d+ tasks?$/ }).first().click();


  // Walk the whole queue by creating each ticket in turn, recording which
  // tickets the intake flow actually opened for. This is the "start 10, get 1"
  // regression stated as an assertion.
  const seen = [];
  for (let i = 0; i < 6; i++) {
    await page.waitForTimeout(500);
    if (await page.locator(".queue-notice-text").count() === 0) break;
    const notice = await noticeText(page);
    const m = notice.match(/NO-\d+/);
    if (m) seen.push(m[0]);
    await page.locator("button", { hasText: /^Next/ }).click();
    await page.waitForTimeout(400);
    await page.getByRole("button", { name: "Create Task" }).click();
  }
  await page.waitForTimeout(500);
  check(
    "all four picked tickets reach the intake flow — none is silently dropped",
    JSON.stringify(seen) === JSON.stringify(["NO-1", "NO-2", "NO-3", "NO-4"]),
    JSON.stringify(seen),
  );
  check("four picked tickets create four tasks", state.created.length === 4, `${state.created.length} created`);
  check("no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── Scenario C: the "Reading NO-1 from Jira…" overlay has a way out ───────
{
  const state = { created: [], grills: 0, detailCalls: [] };
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await stubApi(page, state, { hangDetailFor: "NO-1" });
  await page.goto("http://127.0.0.1:4622/", { waitUntil: "networkidle" });
  await page.locator(".nh-sidenav button", { hasText: "Backlog" }).click();
  await page.waitForTimeout(400);
  await page.getByRole("button", { name: "Select all" }).click();
  await page.locator("button", { hasText: /^Start \d+ tasks?$/ }).first().click();
  await page.waitForTimeout(600);

  const reading = await page.locator(".grill-loading-text").first().textContent();
  check("a slow tracker shows the reading overlay", /Reading NO-1/.test(reading), reading);
  const cancels = await page.locator(".new-task-modal button", { hasText: /^Cancel$/ }).count();
  check("the reading overlay offers a Cancel — every other step of this flow has one", cancels === 1, `${cancels} cancel buttons`);

  await page.keyboard.press("Escape");
  await page.waitForTimeout(700);
  const after = await noticeText(page);
  check(
    "Escape escapes the reading overlay, skipping that ticket and keeping the rest",
    after === "Ticket 2 of 4 · NO-2",
    after,
  );
  check("no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── Scenario D: both trackers listed together (P1-4) ─────────────────────
// The page used to say "the Linear side has no issue listing yet" — false when
// it was written, and the reason the route was never built. This drives the
// real page against both endpoints: Linear rows must arrive, be startable, and
// resolve through the LINEAR detail route with source="linear".
{
  const state = { created: [], grills: 0, detailCalls: [] };
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await stubApi(page, state, { trackers: ["jira", "linear"] });
  await page.goto("http://127.0.0.1:4622/", { waitUntil: "networkidle" });
  await page.locator(".nh-sidenav button", { hasText: "Backlog" }).click();
  await page.waitForTimeout(600);

  const body = await page.locator(".outcome-page").innerText();
  check("Linear tickets are listed on the page", /LIN-7: Only on Linear/.test(body), "");
  check("Jira tickets are still listed beside them", /NO-2: Ticket number 2/.test(body), "");

  const sources = await page.locator("p", { hasText: /^Open tickets from/ }).first().textContent();
  check(
    "the sources line names both trackers and claims nothing about either",
    sources.trim() === "Open tickets from Jira and Linear.",
    sources.trim(),
  );
  check(
    "the false 'Linear has no issue listing' explanation is gone from the page",
    !/no issue listing|Linear is not connected|Jira only/i.test(body),
    body.slice(0, 120),
  );

  // Both trackers hold a NO-1. They are two rows and two checkboxes; ticking
  // one must not tick the other.
  const boxes = page.locator('input[type="checkbox"]');
  check("both NO-1 rows are listed — a shared key is not one ticket", await boxes.count() === 6, `${await boxes.count()} rows`);

  // Rows are newest-first across the merged list, so the Linear NO-1
  // (2026-08-04) leads and its checkbox is the first one.
  await boxes.first().check();
  const startLbl = await page.locator("button", { hasText: /^Start \d+ tasks?$/ }).first().textContent();
  check("ticking one row of a shared key starts exactly one task", startLbl.trim() === "Start 1 task", startLbl);

  await page.locator("button", { hasText: /^Start \d+ tasks?$/ }).first().click();
  await page.waitForTimeout(600);
  check(
    "the picked Linear ticket is fetched from the LINEAR detail route",
    state.detailCalls.includes("linear:NO-1") && !state.detailCalls.includes("jira:NO-1"),
    JSON.stringify(state.detailCalls),
  );
  const prompt = await page.locator("textarea").first().inputValue();
  check("the composer is prefilled from the Linear ticket", prompt.includes("FULL body of NO-1"), prompt.slice(0, 40));

  await page.locator("button", { hasText: /^Next/ }).click();
  await page.waitForTimeout(400);
  await page.getByRole("button", { name: "Create Task" }).click();
  await page.waitForTimeout(600);
  check(
    "the created task is stamped source=linear, not jira",
    state.created[0]?.source === "linear" && state.created[0]?.external_id === "NO-1",
    JSON.stringify(state.created[0] || null),
  );
  check("no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── Scenario E: one tracker down, the other still readable (P1-5) ────────
{
  const state = { created: [], grills: 0, detailCalls: [] };
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await stubApi(page, state, { trackers: ["jira", "linear"], failTracker: "linear" });
  await page.goto("http://127.0.0.1:4622/", { waitUntil: "networkidle" });
  await page.locator(".nh-sidenav button", { hasText: "Backlog" }).click();
  await page.waitForTimeout(700);

  const alert = await page.locator('[role="alert"]').first().textContent();
  check("the failing tracker is named, and the failure is not called an empty backlog",
    /Couldn.t reach Linear/.test(alert) && /not empty/.test(alert), alert.trim().slice(0, 90));
  check("the tracker that DID answer still shows its tickets",
    /NO-2: Ticket number 2/.test(await page.locator(".outcome-page").innerText()), "");
  check("a failing tracker is never reported as 'not configured'",
    !/not configured/i.test(await page.locator(".outcome-page").innerText()), "");
  check("no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── Scenario F: the server itself is down (P1-5) ─────────────────────────
// fetchIntegrations used to fold ANY non-ok response into `{integrations: []}`,
// which is what a healthy server saying "nothing configured" looks like. So a
// 500 rendered "Jira is not configured" — the exact inverse of the rule this
// page states — and sent the operator hunting for an API token that was fine.
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("**/api/**", (route) => {
    const u = route.request().url();
    const json = (b, status = 200) =>
      route.fulfill({ status, contentType: "application/json", body: JSON.stringify(b) });
    if (u.match(/\/api\/integrations($|\?)/)) return json({ detail: "database is locked" }, 500);
    if (u.includes("/api/onboarding")) return json({ completed: true });
    if (u.includes("/api/tasks")) return json([]);
    if (u.includes("/api/projects")) return json([]);
    return json({});
  });
  await page.goto("http://127.0.0.1:4622/", { waitUntil: "networkidle" });
  await page.locator(".nh-sidenav button", { hasText: "Backlog" }).click();
  await page.waitForTimeout(700);

  const body = await page.locator(".outcome-page").innerText();
  check(
    "a 500 from the server is NOT reported as an unconfigured integration",
    !/not configured/i.test(body) && !/JIRA_API_TOKEN/.test(body),
    body.replace(/\n+/g, " | ").slice(0, 140),
  );
  check(
    "it says the server could not be reached, and shows the reason",
    /Couldn.t reach the no_human server/.test(body) && /database is locked/.test(body),
    body.replace(/\n+/g, " | ").slice(0, 140),
  );
  check("and offers a retry", await page.getByRole("button", { name: "Try again" }).count() === 1, "");
  check("no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
