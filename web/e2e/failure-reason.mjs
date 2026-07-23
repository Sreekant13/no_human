// The failed-task drawer must surface the orchestrator's failure_reason verbatim,
// so a quota stop ("...returned an error result: success") is distinguishable
// from a real capability failure — it previously showed only a Retry button.
// Drives the real Failed outcome page + drawer against a mocked API (no :8420).
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
await new Promise((r) => srv.listen(4690, r));

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};

const browser = await chromium.launch();
const REASON = "agent run did not complete: Claude Code returned an error result: success";
const mkFailed = (extra = {}) => ({
  id: "t1", title: "Add analytics export endpoint", status: "failed", kind: "feature",
  description: "Some description", created_at: "2026-07-23T09:00:00Z",
  updated_at: "2026-07-23T09:30:00Z", total_tokens: 1_200_000, attempts: 1, ...extra,
});

async function openFailedDrawer(task) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("**/api/**", (route) => {
    const u = route.request().url();
    const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
    if (route.request().method() !== "GET") return j({});
    if (u.match(/\/api\/tasks\/[^/]+\/diff/)) return j({ diff: "" });
    if (u.match(/\/api\/tasks\/[^/]+\/events/)) return j([]);
    if (u.match(/\/api\/tasks\/[^/?]+$/)) return j(task);   // detail (drives the drawer)
    if (u.includes("/api/tasks")) return j([task]);          // list
    if (u.includes("/api/onboarding")) return j({ completed: true });
    if (u.includes("/api/metrics")) return j({});
    if (u.includes("/api/projects")) return j([]);
    return j({});
  });
  await page.goto("http://127.0.0.1:4690/", { waitUntil: "networkidle" });
  await page.waitForTimeout(300);
  await page.locator(".nh-navrow", { hasText: "Failed" }).first().click();
  await page.waitForTimeout(300);
  await page.locator(".stats-tr-clickable").first().click();
  await page.locator(".slideover").waitFor({ state: "visible", timeout: 5000 }).catch(() => {});
  return { ctx, page, errors };
}

// ── With a failure_reason: the banner shows it verbatim, styled as an alert ──
{
  const { ctx, page, errors } = await openFailedDrawer(mkFailed({ failure_reason: REASON }));
  const drawer = page.locator(".slideover");
  check("[A] the failed task drawer opened", await drawer.isVisible().catch(() => false));
  const banner = page.locator(".so-failure");
  check("[A] the why-it-failed banner is shown", await banner.isVisible().catch(() => false));
  check("[A] the failure reason is rendered VERBATIM",
    (await banner.innerText().catch(() => "")).includes(REASON), "");
  // Computed style: it must actually read as an alert (undefined tokens would
  // paint nothing — assert the real border/background, not just the class).
  const style = await page.evaluate(() => {
    const el = document.querySelector(".so-failure");
    if (!el) return null;
    const s = getComputedStyle(el);
    return { bw: s.borderTopWidth, bg: s.backgroundColor };
  });
  check("[A] the banner has a visible border (computed)", style && style.bw !== "0px", JSON.stringify(style));
  check("[A] the banner has a non-transparent background (computed)",
    style && style.bg !== "rgba(0, 0, 0, 0)" && style.bg !== "transparent", JSON.stringify(style && style.bg));
  check("[A] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── Without a failure_reason: drawer opens, banner is absent (gated) ─────────
{
  const { ctx, page, errors } = await openFailedDrawer(mkFailed());   // no failure_reason
  check("[B] the failed task drawer still opened (positive gate)",
    await page.locator(".slideover").isVisible().catch(() => false));
  check("[B] no banner when there is no failure_reason",
    (await page.locator(".so-failure").count()) === 0);
  check("[B] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
