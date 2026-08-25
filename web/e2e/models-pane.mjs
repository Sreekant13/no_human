// Settings > Models (model picker part 3 of 3): one row per role, fed by
// GET /api/models, writes going through PUT /api/config/models only. Drives
// the real UI against a mocked models API (never touches :8420 or the real
// ~/.no_human/config.yaml). Mirrors settings-account.mjs's structure.
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
await new Promise((r) => srv.listen(4671, r));

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};

const browser = await chromium.launch();

// Fixture ids/notes are test-only strings, not literals copied from the
// production catalog — the point is that the pane renders whatever the
// server sends, not a hardcoded five.
const VENDOR_PIN_NOTE = "only Claude ids may run this role.";
const DISABLED_REASON = "'gpt-5-codex' cannot be set as llm.primary_model: only the Claude backend reads that key (worker.backend).";
const COST_NOTE = "The operator's 2026-08-11 A/B reverted this role from claude-opus-5 back to claude-opus-4-8.";

function basePayload(over = {}) {
  return {
    roles: [
      {
        role: "coder", key: "primary_model", current: "claude-sonnet-5", default: "claude-sonnet-5",
        note: "", cost_note: "",
        options: [
          { id: "claude-sonnet-5", price_class: { label: "medium" }, is_default: true, note: "", requires_backend: false, disabled_reason: "" },
          { id: "gpt-5-codex", price_class: { label: "medium" }, is_default: false, note: "", requires_backend: true, disabled_reason: DISABLED_REASON },
        ],
      },
      {
        role: "reviewer", key: "review_model", current: "claude-opus-4-8", default: "claude-opus-4-8",
        note: VENDOR_PIN_NOTE, cost_note: COST_NOTE,
        options: [
          { id: "claude-opus-4-8", price_class: { label: "high" }, is_default: true, note: VENDOR_PIN_NOTE, requires_backend: false, disabled_reason: "" },
          { id: "claude-opus-5", price_class: { label: "high" }, is_default: false, note: VENDOR_PIN_NOTE, requires_backend: false, disabled_reason: "" },
        ],
      },
      {
        role: "planner", key: "planner_model", current: "claude-opus-5", default: "claude-opus-5",
        note: VENDOR_PIN_NOTE, cost_note: "",
        options: [{ id: "claude-opus-5", price_class: { label: "high" }, is_default: true, note: VENDOR_PIN_NOTE, requires_backend: false, disabled_reason: "" }],
      },
      {
        role: "supervisor", key: "supervisor_model", current: "claude-sonnet-5", default: "claude-sonnet-5",
        note: VENDOR_PIN_NOTE, cost_note: "",
        options: [{ id: "claude-sonnet-5", price_class: { label: "medium" }, is_default: true, note: VENDOR_PIN_NOTE, requires_backend: false, disabled_reason: "" }],
      },
      {
        role: "utility", key: "utility_model", current: "claude-haiku-4-5", default: "claude-haiku-4-5",
        note: VENDOR_PIN_NOTE, cost_note: "",
        options: [{ id: "claude-haiku-4-5", price_class: { label: "low" }, is_default: true, note: VENDOR_PIN_NOTE, requires_backend: false, disabled_reason: "" }],
      },
    ],
    restart_required: false,
    ...over,
  };
}

// Open Settings and switch to the Models section. `page.route("**/api/**")`
// must already be installed. Returns the page.
async function openModels(page) {
  await page.goto("http://127.0.0.1:4671/", { waitUntil: "networkidle" });
  await page.waitForTimeout(300);
  await page.getByRole("button", { name: /^Settings$/ }).click();
  await page.waitForTimeout(300);
  await page.getByRole("button", { name: /^Models$/ }).click();
  await page.waitForTimeout(300);
}

function commonRoutes(j) {
  return (route) => {
    const u = route.request().url();
    if (u.includes("/api/onboarding")) return j({ completed: true });
    if (u.includes("/api/tasks")) return j([]);
    if (u.includes("/api/projects")) return j([]);
    return j({});
  };
}

// ── Scenario 1: initial render — 5 rows, chips, defaults, disabled option ────
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("**/api/**", (route) => {
    const u = route.request().url();
    const j = (b, s = 200) => route.fulfill({ status: s, contentType: "application/json", body: JSON.stringify(b) });
    if (u.includes("/api/models") && route.request().method() === "GET") return j(basePayload());
    return commonRoutes(j)(route);
  });
  await openModels(page);

  const rows = page.locator(".models-row");
  check("[s1] exactly five rows are rendered", (await rows.count()) === 5, String(await rows.count()));

  const labels = await page.locator(".models-row .auth-label").allInnerTexts();
  check("[s1] rows are labelled Coder/Reviewer/Planner/Supervisor/Utility in order",
    labels.every((t, i) => t.startsWith(["Coder", "Reviewer", "Planner", "Supervisor", "Utility"][i])),
    JSON.stringify(labels));

  const chips = await page.locator(".models-row .integration-chip").allInnerTexts();
  check("[s1] every row shows a price-class chip", chips.length === 5 && chips.every(Boolean), JSON.stringify(chips));

  const defaults = await page.locator(".models-row .models-default code").allInnerTexts();
  check("[s1] every row shows its default id", JSON.stringify(defaults) ===
    JSON.stringify(["claude-sonnet-5", "claude-opus-4-8", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]),
    JSON.stringify(defaults));

  const coderSelect = rows.nth(0).locator("select");
  const gptOption = coderSelect.locator('option[value="gpt-5-codex"]');
  // Playwright's isDisabled() locator assertion does not treat <option> as a
  // form control, so it reads the DOM property directly instead.
  check("[s1] the coder's requires_backend option is disabled",
    await gptOption.evaluate((el) => el.disabled) === true);
  check("[s1] the disabled option's title is the server's disabled_reason",
    (await gptOption.getAttribute("title")) === DISABLED_REASON,
    await gptOption.getAttribute("title"));

  check("[s1] the reviewer row shows the reviewer cost_note",
    (await rows.nth(1).innerText()).includes(COST_NOTE));
  check("[s1] the coder row shows no cost_note", !(await rows.nth(0).innerText()).includes("A/B"));

  check("[s1] no restart banner on a fresh load",
    !(await page.locator(".nh-alarm.auth-alarm").isVisible().catch(() => false)));
  check("[s1] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── Scenario 2: change reviewer, Save, restart banner, PUT body scoped ───────
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  let putBody = null;
  await page.route("**/api/**", (route) => {
    const req = route.request(); const u = req.url();
    const j = (b, s = 200) => route.fulfill({ status: s, contentType: "application/json", body: JSON.stringify(b) });
    if (u.includes("/api/models") && req.method() === "GET") return j(basePayload());
    if (u.includes("/api/config/models") && req.method() === "PUT") {
      putBody = JSON.parse(req.postData() || "{}");
      return j(basePayload({ restart_required: true, roles: basePayload().roles.map((r) =>
        r.role === "reviewer" ? { ...r, current: "claude-opus-5" } : r) }));
    }
    return commonRoutes(j)(route);
  });
  await openModels(page);

  const rows = page.locator(".models-row");
  await rows.nth(1).locator("select").selectOption("claude-opus-5");
  await page.getByRole("button", { name: /^Save$/ }).click();
  await page.waitForTimeout(400);

  check("[s2] the PUT body contains ONLY the changed key",
    putBody && Object.keys(putBody).length === 1 && putBody.review_model === "claude-opus-5",
    JSON.stringify(putBody));
  check("[s2] the restart banner is shown after a restart_required response",
    await page.locator(".nh-alarm.auth-alarm").isVisible().catch(() => false));
  check("[s2] the restart banner names the restart command",
    (await page.locator(".nh-alarm.auth-alarm").innerText().catch(() => "")).includes("nh stop && nh start"));
  check("[s2] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── Scenario 3: Reset to defaults ─────────────────────────────────────────────
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const drifted = basePayload({
    roles: basePayload().roles.map((r) =>
      r.role === "reviewer" ? { ...r, current: "claude-opus-5" } : r),
  });
  let putBody = null;
  await page.route("**/api/**", (route) => {
    const req = route.request(); const u = req.url();
    const j = (b, s = 200) => route.fulfill({ status: s, contentType: "application/json", body: JSON.stringify(b) });
    if (u.includes("/api/models") && req.method() === "GET") return j(drifted);
    if (u.includes("/api/config/models") && req.method() === "PUT") {
      putBody = JSON.parse(req.postData() || "{}");
      return j(basePayload({ restart_required: false }));
    }
    return commonRoutes(j)(route);
  });
  await openModels(page);
  await page.getByRole("button", { name: /^Reset to defaults$/ }).click();
  await page.waitForTimeout(400);

  check("[s3] the PUT body equals the payload's own defaults for the drifted role",
    putBody && Object.keys(putBody).length === 1 && putBody.review_model === "claude-opus-4-8",
    JSON.stringify(putBody));

  const values = await page.locator(".models-row select").evaluateAll((els) => els.map((e) => e.value));
  check("[s3] every select shows its default after Reset",
    JSON.stringify(values) === JSON.stringify(["claude-sonnet-5", "claude-opus-4-8", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]),
    JSON.stringify(values));
  check("[s3] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── Scenario 4: a 422 refusal reverts every pending edit, shows the detail ────
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const DETAIL = "'gpt-5.4' has no published price; refusing to run an unpriced model.";
  await page.route("**/api/**", (route) => {
    const req = route.request(); const u = req.url();
    const j = (b, s = 200) => route.fulfill({ status: s, contentType: "application/json", body: JSON.stringify(b) });
    if (u.includes("/api/models") && req.method() === "GET") return j(basePayload());
    if (u.includes("/api/config/models") && req.method() === "PUT") return j({ detail: DETAIL }, 422);
    return commonRoutes(j)(route);
  });
  await openModels(page);

  const rows = page.locator(".models-row");
  await rows.nth(1).locator("select").selectOption("claude-opus-5");
  await page.getByRole("button", { name: /^Save$/ }).click();
  await page.waitForTimeout(400);

  check("[s4] the server's refusal detail is rendered verbatim",
    (await page.locator(".settings-error").innerText().catch(() => "")).includes(DETAIL),
    await page.locator(".settings-error").innerText().catch(() => "(none)"));
  const reviewerValue = await rows.nth(1).locator("select").inputValue();
  check("[s4] the reviewer select reverted to `current`, not the failed pending edit",
    reviewerValue === "claude-opus-4-8", reviewerValue);
  check("[s4] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── Scenario 5: endpoint absent — degrades to an 'unavailable' note ──────────
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("**/api/**", (route) => {
    const u = route.request().url();
    const j = (b, s = 200) => route.fulfill({ status: s, contentType: "application/json", body: JSON.stringify(b) });
    if (u.includes("/api/models") && route.request().method() === "GET") {
      return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
    }
    return commonRoutes(j)(route);
  });
  await openModels(page);
  const noteVisible = (await page.locator(".settings-empty").innerText().catch(() => "")).match(/unavailable/i) != null;
  check("[s5] the panel degrades to an 'unavailable' note when the endpoint is absent", noteVisible);
  check("[s5] with the unavailable note shown, no rows are rendered",
    noteVisible && (await page.locator(".models-row").count()) === 0);
  check("[s5] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
