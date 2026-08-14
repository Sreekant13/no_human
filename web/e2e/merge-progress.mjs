// Approve-and-merge live progress (operator finding: a 2-4 minute synchronous
// land with zero feedback read as "doesn't work"). Drives the BUILT bundle
// (web/dist), with a fake, test-controllable `window.WebSocket` — the merge
// progress frames (`task_event`: merge_started/merge_step_*/human_merged)
// ride the existing broadcast socket, so this stub is what lets a test push
// them without a real backend.
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
await new Promise((r) => srv.listen(4691, r));

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};

const now = new Date().toISOString();
const TASK = {
  id: "mergeprogress00aaaabbbb",
  title: "Approve and merge shows live progress",
  status: "awaiting_approval",
  kind: "feature",
  description: "Some description",
  created_at: now, updated_at: now,
  total_tokens: 1_000, attempts: 1,
  pr_url: "https://example.com/pull/7",
};

// The fake WebSocket: exposes every constructed instance on
// `window.__nhWS` so the Node side can push frames through
// `ws.__emit(frameObject)` — a controllable double of the real broadcast
// socket, not a real server.
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
        setTimeout(() => {
          this.readyState = 1;
          if (this.onopen) this.onopen();
        }, 0);
      }
      send() {}
      close() {
        this.readyState = 3;
        if (this.onclose) this.onclose();
      }
      __emit(obj) {
        if (this.onmessage) this.onmessage({ data: JSON.stringify(obj) });
      }
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

function mockApi(page, { approveDelayMs = 0, approveStatus = 200, approveBody = null } = {}) {
  let approveCount = 0;
  const getApproveCount = () => approveCount;
  const handler = async (route) => {
    const req = route.request();
    const u = req.url();
    const j = (b, status = 200) =>
      route.fulfill({ status, contentType: "application/json", body: JSON.stringify(b) });
    if (req.method() === "POST" && u.includes("/approve") && !u.includes("/approve-landed")) {
      approveCount += 1;
      if (approveDelayMs) await new Promise((r) => setTimeout(r, approveDelayMs));
      const body = approveBody ?? {
        ok: true, landed_sha: "abc123def456",
        message: "Approved and merged — landed abc123def456 onto the default branch.",
      };
      return j(body, approveStatus);
    }
    if (req.method() !== "GET") return j({});
    if (u.includes("/api/onboarding")) return j({ completed: true });
    if (u.includes("/api/projects")) return j([]);
    if (u.match(/\/api\/tasks\/[^/]+\/diff/)) return j({ diff: "" });
    if (u.match(/\/api\/tasks\/[^/]+\/events/)) return j([]);
    if (u.match(/\/api\/tasks\/[^/]+$/)) return j(TASK);
    if (u.includes("/api/tasks")) return j([TASK]);
    return j({});
  };
  return { handler, getApproveCount };
}

const browser = await chromium.launch();

async function openDrawer(opts = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("console", (m) => { if (m.type() === "error" && !m.text().includes("WebSocket")) errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  await stubWebSocket(page);
  const mock = mockApi(page, opts);
  await page.route("**/api/**", mock.handler);
  await page.goto("http://127.0.0.1:4691/", { waitUntil: "networkidle" });
  await page.waitForTimeout(400);
  await page.locator(".task-card").first().click();
  await page.locator(".slideover").waitFor({ state: "visible", timeout: 5000 });
  await page.waitForTimeout(300);
  return { ctx, page, errors, mock };
}

// 9 — click → feedback latency: the button must flip to "Merging…" and
// disable itself within 100ms of the click, well before the (here, 3s)
// server round-trip resolves.
{
  const { ctx, page, errors } = await openDrawer({ approveDelayMs: 3000 });
  const btn = page.locator(".btn-approve");
  // Measured entirely IN-BROWSER (performance.now() either side of the
  // click + a MutationObserver on the button), not round-tripped through
  // Node/CDP — a Node-side Date.now() around Playwright's own click()/
  // waitFor() calls bundles in automation-protocol latency that has nothing
  // to do with the React state-change latency this criterion is about.
  const elapsed = await page.evaluate(() => new Promise((resolve) => {
    const el = document.querySelector(".btn-approve");
    const t0 = performance.now();
    const done = () => resolve(performance.now() - t0);
    const obs = new MutationObserver(() => {
      if (el.textContent.includes("Merging")) { obs.disconnect(); done(); }
    });
    obs.observe(el, { childList: true, characterData: true, subtree: true });
    el.click();
    if (el.textContent.includes("Merging")) { obs.disconnect(); done(); }
  }));
  check("[latency] button shows Merging… within 100ms of click", elapsed < 100, `${elapsed.toFixed(2)}ms`);
  check("[latency] button is disabled while merging", await btn.isDisabled());
  check("[latency] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// 10 — rapid double-click: exactly one POST /approve, even though the
// button is clicked twice ~20ms apart (a raced double-click beating the
// disable). The frontend guard is the button's own disabled state.
{
  const { ctx, page, mock } = await openDrawer({ approveDelayMs: 1000 });
  await page.evaluate(() => {
    const btn = document.querySelector(".btn-approve");
    btn.click();
    setTimeout(() => btn.click(), 20);
  });
  await page.waitForTimeout(300);
  check("[double-click] exactly one POST /approve reached the server",
    mock.getApproveCount() === 1, `saw ${mock.getApproveCount()}`);
  await ctx.close();
}

// 11 — streamed events: task_event frames pushed through the fake socket
// render the current step, and the resolved POST flips the label to
// "Approved — merge pending" — with no window where the label reads the
// plain idle "Approve and merge" after the click.
{
  const { ctx, page, errors } = await openDrawer({ approveDelayMs: 400 });
  const btn = page.locator(".btn-approve");
  const labelsSeen = [];
  const poll = setInterval(() => {
    btn.innerText().then((t) => labelsSeen.push(t)).catch(() => {});
  }, 15);

  await btn.click();
  // The `connectTaskProgress` socket opens from a useEffect keyed on
  // `merging` — give React a tick to commit and run it before pushing
  // frames, or `window.__nhWS`'s last entry could still be the board's own
  // WS from mount.
  await page.waitForTimeout(50);
  await pushWSFrame(page, {
    type: "task_event", task_id: TASK.id,
    event: { source: "human", kind: "merge_started", text: "merge started", ts: 1 },
  });
  await page.waitForTimeout(80);
  await pushWSFrame(page, {
    type: "task_event", task_id: TASK.id,
    event: { source: "human", kind: "merge_step_squash", text: "merge: squash", ts: 2 },
  });
  await page.waitForTimeout(80);
  const stepEl = page.locator(".approve-merge-step");
  const midText = await stepEl.innerText().catch(() => "");
  check("[stream] a merge_step_* frame renders its step name", /squash/i.test(midText), midText);

  await pushWSFrame(page, {
    type: "task_event", task_id: TASK.id,
    event: { source: "human", kind: "merge_step_push", text: "merge: push", ts: 3 },
  });
  await page.waitForTimeout(80);
  const pushText = await stepEl.innerText().catch(() => "");
  check("[stream] a later merge_step_* frame updates the step name", /push/i.test(pushText), pushText);

  await pushWSFrame(page, {
    type: "task_event", task_id: TASK.id,
    event: { source: "human", kind: "human_merged", text: "landed", ts: 4 },
  });
  await page.waitForTimeout(700); // let the mocked POST resolve
  clearInterval(poll);
  const finalText = await btn.innerText();
  check("[stream] the button transitions to Approved — merge pending on completion",
    /Approved.*merge pending/i.test(finalText), finalText);
  check("[stream] no dead-button window: label never reads the plain idle text after the click",
    !labelsSeen.some((t) => t.trim() === "Approve and merge"), JSON.stringify(labelsSeen.slice(0, 10)));
  check("[stream] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// 12 — failure: a land failure surfaces a persistent inline banner naming
// the step and the stderr, which stays up until the operator dismisses it.
{
  const { ctx, page } = await openDrawer({
    approveStatus: 500,
    approveBody: { detail: { step: "push", stderr: "remote: rejected — non-fast-forward, retry after fetch" } },
  });
  await page.locator(".btn-approve").click();
  const banner = page.locator(".flash-banner");
  await banner.waitFor({ state: "visible", timeout: 3000 });
  const text = await banner.innerText();
  check("[failure] banner names the failed step", /Failed: push/.test(text), text);
  check("[failure] banner includes the stderr text", text.includes("non-fast-forward"), text);

  await page.waitForTimeout(2000);
  check("[failure] banner persists (not auto-dismissed)", await banner.isVisible().catch(() => false));

  await page.locator(".flash-banner-dismiss").click();
  const stillVisible = await banner.isVisible().catch(() => false);
  check("[failure] banner disappears only after the dismiss click", !stillVisible);
  await ctx.close();
}

await browser.close();
srv.close();

console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nmerge-progress: all checks passed");
process.exit(failures.length ? 1 : 0);
