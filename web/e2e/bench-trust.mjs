// The Stats bench instrument-trust card (Task 2). Drives the real Stats page
// against a MOCKED /api/bench/latest (a separate backend PR builds the endpoint;
// this never touches :8420). Verifies the three legibility properties: a refused
// run alerts with its reason verbatim, an unmeasured/under-corpus run alerts, and
// the honest-escalation DENOMINATOR is shown so 2/2 != 14/14.
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
await new Promise((r) => srv.listen(4680, r));

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};

const browser = await chromium.launch();
const TASK = { id: "t1", title: "a task", status: "done", kind: "code",
  created_at: "2026-07-20T10:00:00Z", updated_at: "2026-07-20T11:00:00Z" };

// The COMPUTED style of the card — asserting the class string alone would pass
// even when the alert tokens are undefined and paint nothing (the real failure
// mode: border collapses to 0px, background to transparent).
const cardStyle = (page) => page.evaluate(() => {
  const el = document.querySelector(".bench-trust");
  if (!el) return null;
  const s = getComputedStyle(el);
  return { borderWidth: s.borderTopWidth, borderColor: s.borderTopColor, bg: s.backgroundColor };
});
let alertStyle = null;
let baseStyle = null;

// Mount the app, mock /api/** with `bench` for /api/bench/latest, land on Stats.
async function openStats(bench) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 1000 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("**/api/**", (route) => {
    const u = route.request().url();
    const j = (b, s = 200) => route.fulfill({ status: s, contentType: "application/json", body: JSON.stringify(b) });
    if (u.includes("/api/bench/latest")) {
      if (bench === "404") return route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ detail: "no run" }) });
      if (bench === "absent") return route.fulfill({ status: 500, contentType: "application/json", body: "{}" });
      return j(bench);
    }
    if (u.includes("/api/onboarding")) return j({ completed: true });
    if (u.includes("/api/tasks")) return j([TASK]);
    if (u.includes("/api/metrics")) return j({ prs_merged: 3, prs_opened: 4, review_pass: 5, review_fail: 1 });
    if (u.includes("/api/repos")) return j([]);
    return j({});
  });
  await page.goto("http://127.0.0.1:4680/", { waitUntil: "networkidle" });
  await page.waitForTimeout(300);
  await page.getByRole("button", { name: /^Stats$/ }).click();
  await page.waitForTimeout(400);
  return { ctx, page, errors };
}

const REFUSED = {
  label: "expanded-core-v13", created_at: "2026-07-23T09:00:00Z",
  total: 10, skipped: 3, dead_specs: 4, corpus_available: 24,
  success_rate: 1.0, satisfied: 3, median_cost_ratio: 0.11,
  honest_escalation_rate: 1.0, honest_escalations: 2, escalation_specs: 2,
  refusals: ["6 of 10 specs burned zero tokens — sub-threshold saturation, not a result."],
  override_reasons: [],
};
const HEALTHY = {
  label: "expanded-core-v14", created_at: "2026-07-23T10:00:00Z",
  total: 24, skipped: 0, dead_specs: 0, corpus_available: 24,
  success_rate: 0.58, satisfied: 14, median_cost_ratio: 0.10,
  honest_escalation_rate: 1.0, honest_escalations: 14, escalation_specs: 14,
  refusals: [], override_reasons: [],
};

// ── Scenario A: a REFUSED, unmeasured, small-sample run — must ALARM ─────────
{
  const { ctx, page, errors } = await openStats(REFUSED);
  const card = page.locator(".bench-trust");
  check("[A] the bench card renders", await card.count() >= 1);
  check("[A] the card is in the ALERT state", (await card.getAttribute("class") || "").includes("bench-trust-alert"));
  alertStyle = await cardStyle(page);
  check("[A] the alert card has a VISIBLE border (computed, not just a class)",
    alertStyle && alertStyle.borderWidth !== "0px", JSON.stringify(alertStyle));
  check("[A] the alert card has a non-transparent background (computed)",
    alertStyle && alertStyle.bg !== "rgba(0, 0, 0, 0)" && alertStyle.bg !== "transparent", JSON.stringify(alertStyle && alertStyle.bg));
  check("[A] a 'refused publication' alarm is shown", await page.locator(".bench-alarm", { hasText: /refused publication/i }).isVisible().catch(() => false));
  check("[A] the refusal reason is rendered VERBATIM",
    (await card.innerText().catch(() => "")).includes("sub-threshold saturation, not a result"),
    "");
  check("[A] the unmeasured count is surfaced (7 of 10)", (await card.innerText().catch(() => "")).includes("7 of 10"));
  check("[A] the under-corpus coverage is surfaced (10 of 24)", (await card.innerText().catch(() => "")).includes("10 of 24"));
  // The crux: the denominator is shown AND flagged small.
  check("[A] honest-escalation shows its denominator (2/2)", (await card.innerText().catch(() => "")).includes("(2/2)"));
  check("[A] a 2-sample escalation rate is flagged small", (await card.innerText().catch(() => "")).toLowerCase().includes("small sample"));
  check("[A] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── Scenario B: a HEALTHY 14/14 run — no alarm, and 14/14 != 2/2 ─────────────
{
  const { ctx, page, errors } = await openStats(HEALTHY);
  const card = page.locator(".bench-trust");
  check("[B] the bench card renders", await card.count() >= 1);           // positive gate before the negatives
  check("[B] the card is NOT in the alert state", !((await card.getAttribute("class") || "").includes("bench-trust-alert")));
  baseStyle = await cardStyle(page);
  check("[B] the base card has a visible border (computed)",
    baseStyle && baseStyle.borderWidth !== "0px", JSON.stringify(baseStyle));
  check("[B] no refusal alarm on a clean run", !(await page.locator(".bench-alarm", { hasText: /refused/i }).isVisible().catch(() => false)));
  check("[B] honest-escalation shows (14/14), distinct from (2/2)",
    (await card.innerText().catch(() => "")).includes("(14/14)") && !(await card.innerText().catch(() => "")).includes("(2/2)"));
  check("[B] a 14-sample rate is NOT flagged small", !(await card.innerText().catch(() => "")).toLowerCase().includes("small sample"));
  check("[B] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── Scenario C: endpoint present, no run recorded (404) ──────────────────────
{
  const { ctx, page, errors } = await openStats("404");
  check("[C] renders a 'no bench run yet' note", await page.locator(".bench-trust", { hasText: /no bench run/i }).isVisible().catch(() => false));
  check("[C] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── Scenario D: endpoint ABSENT (older build) — the section hides gracefully ──
{
  const { ctx, page, errors } = await openStats("absent");
  // Positive gate first: the Stats page itself rendered (else "hidden" is vacuous).
  check("[D] the Stats page rendered (positive gate)", await page.locator(".stats-northstar, .stats-cards").first().isVisible().catch(() => false));
  check("[D] the bench card is hidden when the endpoint is absent", (await page.locator(".bench-trust").count()) === 0);
  check("[D] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// The at-a-glance distinction the feature exists for: a refused/unmeasured run
// must be VISUALLY different from a healthy one, not just carry a different class.
check("[A vs B] the alert card is visually distinct from the base card (border colour differs)",
  alertStyle && baseStyle && alertStyle.borderColor !== baseStyle.borderColor,
  `alert=${alertStyle && alertStyle.borderColor} base=${baseStyle && baseStyle.borderColor}`);

await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
