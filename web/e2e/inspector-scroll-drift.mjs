// G-2: the Details & tools inspector's scroll position drifts while live
// events append. REPRODUCE FIRST (the plan's rule) — this drives the REAL
// mechanism: the primary digest (ActivityTab) lives ABOVE the accordion
// inside the one shared `.so-scroll` region (styles.css FINDING G), and as
// live events stream in (the SSE path `useTaskEvents` opens for an active,
// claimed task) that digest grows — a turn counter and a live status line
// appear where there were none. If nothing compensates, a reader who has
// scrolled PAST the digest into the accordion below gets visually yanked as
// the document reflows underneath them, even though their `scrollTop` never
// changed.
//
// Three scenarios, matching the fix's actual documented conditions:
//   A. fully scrolled past the digest      -> compensate, zero drift.
//   B. digest still PARTIALLY visible      -> do NOT compensate; the growth
//      must actually become visible, not get scrolled away from the reader.
//   C. a live task REFETCH lands mid-session (Board's WS `sync` path bumping
//      `refreshKey`, a fresh `task` object landing) while the reader stays
//      scrolled fully past -> still zero drift. This is the case an effect
//      keyed on `[task]` (instead of `Boolean(task)`) fails: it tears the
//      ResizeObserver down and re-arms it on every refetch, resetting its
//      internal baseline right as a delta needs to land — here, the delta IS
//      the refetch itself (the new task carries a pr_url the digest didn't
//      render before), so the swallow is immediate and total, not a race.
//
// Unlike drawer.mjs (which mocks every endpoint via page.route), this suite
// runs its OWN http server so the events/stream endpoint can be a REAL,
// long-lived SSE connection the test pushes frames into on demand — a
// route.fulfill() can only answer once, and an in-process Map lets the test
// read server-side counters (e.g. "was this task really refetched?") directly.
import { chromium } from "playwright";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";

const DIST = new URL("../dist", import.meta.url).pathname;
const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };
const PORT = 4652;

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};

// ── a tiny multi-task fixture server ────────────────────────────────────────
const registry = new Map();   // taskId -> { task, events, sseClients, getHits }

function registerTask(id, overrides = {}) {
  const now = new Date().toISOString();
  // A running, CLAIMED task — isActive && claimed is what flips useTaskEvents
  // onto the SSE path (the poll path never "appends" the way the report
  // describes). No blocker, so `defaultOpenSection` opens "system" — a
  // section with real content, so `.so-scroll` genuinely overflows a modest
  // viewport.
  const task = {
    id, title: "Add --json flag to nh status for machine-readable lane counts",
    status: "implementing", kind: "feature", claimed: true,
    description: "Some description", created_at: now, updated_at: now,
    total_tokens: 12_000, attempts: 1, ...overrides,
  };
  registry.set(id, { task, events: [], sseClients: new Set(), getHits: 0 });
  return task;
}

function pushEvent(id, ev) {
  const rec = registry.get(id);
  rec.events.push(ev);
  for (const res of rec.sseClients) res.write(`data: ${JSON.stringify(ev)}\n\n`);
}

// A controllable double of the real broadcast socket — reused from
// merge-progress.mjs's own `stubWebSocket`/`pushWSFrame` idiom, the
// established pattern in this suite for driving Board's WS-`sync` path
// deterministically (App.jsx `connectWS`: `if (msg.tasks) dispatch({type:
// "sync", tasks: msg.tasks})`). `/api/tasks` itself is fetched ONCE on
// mount, never polled — a live update to the board's task list travels over
// this socket, not a REST poll (the REST poll App.jsx runs every ~10s is
// worker-status/queue-health, a separate thing) — so this is the actual
// production path a task refetch takes, not a stand-in for it.
async function stubWebSocket(page) {
  await page.addInitScript(() => {
    class FakeWebSocket {
      constructor(url) {
        this.url = url;
        this.readyState = 0;
        this.onopen = null;
        this.onmessage = null;
        this.onclose = null;
        this.onerror = null;
        window.__nhWS = window.__nhWS || [];
        window.__nhWS.push(this);
        setTimeout(() => { this.readyState = 1; if (this.onopen) this.onopen(); }, 0);
      }
      send() {}
      close() { this.readyState = 3; if (this.onclose) this.onclose(); }
      __emit(obj) { if (this.onmessage) this.onmessage({ data: JSON.stringify(obj) }); }
    }
    window.WebSocket = FakeWebSocket;
  });
}

async function pushWSFrame(page, frame) {
  await page.evaluate((f) => {
    const ws = (window.__nhWS || [])[(window.__nhWS || []).length - 1];
    if (ws) ws.__emit(f);
  }, frame);
}

const srv = http.createServer((req, res) => {
  const u = req.url.split("?")[0];
  const json = (body, status = 200) => {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(body));
  };
  const m = u.match(/^\/api\/tasks\/([^/]+)(\/.*)?$/);

  if (m && m[2] === "/events/stream") {
    const rec = registry.get(m[1]);
    res.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      "Connection": "keep-alive",
    });
    res.write(":ok\n\n");
    rec.sseClients.add(res);
    req.on("close", () => rec.sseClients.delete(res));
    return;
  }
  if (m && m[2] === "/events") return json(registry.get(m[1]).events);
  if (m && m[2] === "/diff") return json({ diff: "" });
  if (u.includes("/api/onboarding")) return json({ completed: true });
  if (u.includes("/api/projects")) return json([]);
  if (m && !m[2]) {
    const rec = registry.get(m[1]);
    rec.getHits += 1;
    return json(rec.task);
  }
  if (u === "/api/tasks") return json([...registry.values()].map((r) => r.task));
  if (u.startsWith("/api/")) return json({});

  // static dist
  let f = path.join(DIST, u === "/" ? "index.html" : u);
  if (!fs.existsSync(f) || fs.statSync(f).isDirectory()) f = path.join(DIST, "index.html");
  res.writeHead(200, { "Content-Type": MIME[path.extname(f)] || "application/octet-stream" });
  res.end(fs.readFileSync(f));
});
await new Promise((r) => srv.listen(PORT, r));

const browser = await chromium.launch();

// Opens the app fresh, opens the given task's drawer, and settles past the
// drawer's own mount animation (transform: scale(0.97→1), ~0.28s) and web
// fonts — both would otherwise read as "drift" that has nothing to do with
// live events.
async function openDrawer({ withFakeWS = false } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 620 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("console", (m) => { if (m.type() === "error" && !m.text().includes("WebSocket")) errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  await page.addInitScript(() => localStorage.setItem("nh-theme", "dark"));
  if (withFakeWS) await stubWebSocket(page);
  await page.goto(`http://127.0.0.1:${PORT}/`, { waitUntil: "networkidle" });
  await page.waitForTimeout(500);
  // `registry.clear()` before each scenario's `registerTask` means exactly
  // one task-card exists at this point — no ambiguity to disambiguate.
  await page.locator(".task-card").first().click();
  await page.locator(".slideover").waitFor({ state: "visible", timeout: 5000 });
  await page.waitForTimeout(700);
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(100);
  return { ctx, page, errors };
}

// The scrollTop at which `.so-primary-stream` is EXACTLY fully scrolled past
// (its bottom edge flush with the scroll region's own top edge). Read at the
// current scrollTop (any value works — the math is offset-invariant).
async function digestFullyPastThreshold(scroller) {
  return scroller.evaluate((el) => {
    const stream = el.querySelector(".so-primary-stream");
    const streamRect = stream.getBoundingClientRect();
    const viewTop = el.getBoundingClientRect().top;
    return (streamRect.bottom - viewTop) + el.scrollTop;
  });
}

// ── Scenario A: fully scrolled past the digest -> compensate, zero drift ───
{
  const TASK_ID = "scenA0aaaabbbbccccdd1";
  registry.clear();   // isolate the board to just this scenario's task
  registerTask(TASK_ID);
  const { ctx, page, errors } = await openDrawer();
  const scroller = page.locator(".slideover .so-scroll");
  check("[A setup] the shared scroll region exists", await scroller.count() === 1);

  const overflowBefore = await scroller.evaluate((el) => ({ scroll: el.scrollHeight, client: el.clientHeight }));
  check("[A setup] the drawer genuinely overflows before any events arrive",
    overflowBefore.scroll > overflowBefore.client,
    `scrollHeight=${overflowBefore.scroll} clientHeight=${overflowBefore.client}`);

  const thresh = await digestFullyPastThreshold(scroller);
  await scroller.evaluate((el, v) => { el.scrollTop = v; }, thresh + 40);
  await page.waitForTimeout(150);
  const scrollTopBefore = await scroller.evaluate((el) => el.scrollTop);
  check("[A setup] the drawer scrolled fully past the digest",
    scrollTopBefore >= thresh, `scrollTop=${scrollTopBefore} threshold=${thresh}`);
  const stillVisible = await scroller.evaluate((el) => {
    const stream = el.querySelector(".so-primary-stream");
    return stream.getBoundingClientRect().bottom > el.getBoundingClientRect().top;
  });
  check("[A setup] …and the digest is genuinely out of view (not merely scrolled 'some')", !stillVisible);

  const anchor = page.locator(".slideover .so-inspector-label");
  check("[A setup] the inspector label anchor is present", await anchor.count() === 1);
  const anchorTopBefore = await anchor.evaluate((el) => el.getBoundingClientRect().top);

  // Drive the REAL live-event path: an attempt starts, then several tool_use
  // events from the coder stream in over the open SSE connection — exactly
  // what makes the primary digest grow (a turn counter + a live status line
  // appear where there were none a moment ago).
  let ts = Math.floor(Date.now() / 1000);
  pushEvent(TASK_ID, { ts: ts++, kind: "attempt_start", max_turns: 40 });
  for (let i = 0; i < 6; i++) {
    pushEvent(TASK_ID, {
      ts: ts++, kind: "tool_use", tool_name: "Edit",
      tool_input: { file_path: `src/module_${i}.py` }, text: `Editing module_${i}.py`,
    });
    await page.waitForTimeout(120);
  }
  await page.waitForTimeout(800);   // let the ResizeObserver's own callback frame settle

  check("[A repro] the primary digest actually grew (turn counter appeared)",
    await page.locator(".slideover .turn-counter").count() > 0);

  const scrollTopAfter = await scroller.evaluate((el) => el.scrollTop);
  const anchorTopAfter = await anchor.evaluate((el) => el.getBoundingClientRect().top);
  const drift = anchorTopAfter - anchorTopBefore;

  // NOTE: scrollTop is EXPECTED to change once this is fixed — the fix
  // compensates it on purpose, by exactly the digest's growth, so the
  // reader's on-screen position (asserted below) holds. Asserting scrollTop
  // stays put would pin the bug, not the fix.
  check("[A repro] scrollTop moved to compensate for the digest's growth (the fix engaged)",
    scrollTopAfter > scrollTopBefore, `before=${scrollTopBefore} after=${scrollTopAfter}`);
  check("[A repro] the inspector anchor does not drift on screen as events append",
    Math.abs(drift) < 4, `anchor moved ${drift}px (before=${anchorTopBefore} after=${anchorTopAfter})`);
  check("[A repro] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── Scenario B: the digest is still PARTIALLY visible -> do NOT compensate,
// and the growth must actually become visible to the reader. A coarse
// `scrollTop > 0` trigger (the review's finding #2) would wrongly compensate
// here too, scrolling the new content away before the reader ever sees it. ──
{
  const TASK_ID = "scenB0aaaabbbbccccdd2";
  registry.clear();
  registerTask(TASK_ID);
  const { ctx, page, errors } = await openDrawer();
  const scroller = page.locator(".slideover .so-scroll");

  const thresh = await digestFullyPastThreshold(scroller);
  const partial = Math.round(thresh / 2);
  await scroller.evaluate((el, v) => { el.scrollTop = v; }, partial);
  await page.waitForTimeout(150);
  const scrollTopBefore = await scroller.evaluate((el) => el.scrollTop);
  check("[B setup] scrolled into the TRANSITIONAL band (0 < scrollTop < threshold)",
    scrollTopBefore > 0 && scrollTopBefore < thresh,
    `scrollTop=${scrollTopBefore} threshold=${thresh}`);
  const digestStillVisible = await scroller.evaluate((el) => {
    const stream = el.querySelector(".so-primary-stream");
    return stream.getBoundingClientRect().bottom > el.getBoundingClientRect().top;
  });
  check("[B setup] …and the digest is genuinely still partly on screen", digestStillVisible);

  let ts = Math.floor(Date.now() / 1000);
  pushEvent(TASK_ID, { ts: ts++, kind: "attempt_start", max_turns: 40 });
  for (let i = 0; i < 6; i++) {
    pushEvent(TASK_ID, {
      ts: ts++, kind: "tool_use", tool_name: "Edit",
      tool_input: { file_path: `src/module_${i}.py` }, text: `Editing module_${i}.py`,
    });
    await page.waitForTimeout(120);
  }
  await page.waitForTimeout(800);

  check("[B repro] the primary digest actually grew (turn counter appeared)",
    await page.locator(".slideover .turn-counter").count() > 0);
  const scrollTopAfter = await scroller.evaluate((el) => el.scrollTop);
  check("[B fix#2] scrollTop is NOT touched while the digest is still (partly) visible",
    scrollTopAfter === scrollTopBefore, `before=${scrollTopBefore} after=${scrollTopAfter}`);
  const turnCounterBox = await page.locator(".slideover .turn-counter").boundingBox();
  const viewport = page.viewportSize();
  const onScreen = !!turnCounterBox && turnCounterBox.y >= 0 && turnCounterBox.y < viewport.height;
  check("[B fix#2] …so the new content that just grew in is actually VISIBLE, not hidden by compensation",
    onScreen, JSON.stringify(turnCounterBox));
  check("[B repro] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── Scenario C: a live task REFETCH lands mid-session (Board's WS `sync`
// path bumping `refreshKey`, handing SlideOver a brand-new `task` object)
// WHILE the reader stays scrolled fully past — the exact case review finding
// #1 named: an effect keyed on `[task]` tears its ResizeObserver down and
// re-arms it on every refetch (task is a fresh object every fetch), resetting
// its baseline right as a delta needs to land, and silently swallowing it.
//
// The refetch itself is made to GROW the digest — the task gains a `pr_url`
// it did not have before, so `artifactsFor` starts rendering a "Pull
// request" row — landing that growth in the SAME React commit as the `task`
// object change, which is exactly the "same tick" hazard finding #1 names.
// A first, ordinary events-driven growth happens BEFORE the refetch too, so
// the observer has real prior state (a non-null `lastHeight`) for the
// refetch to potentially clobber. ──
{
  const TASK_ID = "scenC0aaaabbbbccccdd3";
  registry.clear();
  registerTask(TASK_ID);
  const rec = registry.get(TASK_ID);
  const { ctx, page, errors } = await openDrawer({ withFakeWS: true });
  const scroller = page.locator(".slideover .so-scroll");

  const thresh = await digestFullyPastThreshold(scroller);
  await scroller.evaluate((el, v) => { el.scrollTop = v; }, thresh + 40);
  await page.waitForTimeout(150);
  const scrollTopBefore = await scroller.evaluate((el) => el.scrollTop);
  check("[C setup] scrolled fully past the digest", scrollTopBefore >= thresh);

  const anchor = page.locator(".slideover .so-inspector-label");
  const anchorTopBefore = await anchor.evaluate((el) => el.getBoundingClientRect().top);
  const hitsBefore = rec.getHits;   // steady-state, after the FakeWS's own onOpen resync

  // Growth #1 — ordinary streamed events, so the observer already holds a
  // real (non-null) `lastHeight` by the time the refetch lands.
  let ts = Math.floor(Date.now() / 1000);
  pushEvent(TASK_ID, { ts: ts++, kind: "attempt_start", max_turns: 40 });
  pushEvent(TASK_ID, {
    ts: ts++, kind: "tool_use", tool_name: "Edit",
    tool_input: { file_path: "src/module_0.py" }, text: "Editing module_0.py",
  });
  await page.waitForTimeout(400);
  check("[C setup] growth #1 (turn counter) landed before the refetch",
    await page.locator(".slideover .turn-counter").count() > 0);

  // The refetch: a NEW `task` object (pr_url now set, updated_at bumped)
  // delivered over Board's WS `sync` path — the actual production mechanism,
  // not a stand-in for it (see `stubWebSocket` above). This is growth #2,
  // landing in the SAME commit as the task-object swap.
  // `artifactsFor`/`prUrlFor` (slideOverSummary.js) read `task.context.pr_url`
  // (or an attempt's `pr_url`), never a bare `task.pr_url`.
  rec.task = {
    ...rec.task,
    context: { ...(rec.task.context || {}), pr_url: "https://example.com/pull/1" },
    updated_at: new Date().toISOString(),
  };
  await pushWSFrame(page, { tasks: [rec.task] });
  await page.waitForTimeout(500);

  check("[C setup] the task was actually REFETCHED via the real WS-sync path (not merely assumed)",
    rec.getHits > hitsBefore, `getHits before=${hitsBefore} after=${rec.getHits}`);
  check("[C repro] growth #2 (the Artifacts/PR row) actually landed",
    await page.locator(".slideover .run-artifacts").count() > 0);

  const scrollTopAfter = await scroller.evaluate((el) => el.scrollTop);
  const anchorTopAfter = await anchor.evaluate((el) => el.getBoundingClientRect().top);
  const drift = anchorTopAfter - anchorTopBefore;
  check("[C repro] scrollTop still moved to compensate, across the refetch",
    scrollTopAfter > scrollTopBefore, `before=${scrollTopBefore} after=${scrollTopAfter}`);
  check("[C fix#1] the inspector anchor does not drift even when a task refetch lands mid-stream",
    Math.abs(drift) < 4, `anchor moved ${drift}px (before=${anchorTopBefore} after=${anchorTopAfter})`);
  check("[C repro] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
