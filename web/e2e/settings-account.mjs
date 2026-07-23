// The Settings > Account panel: auth status, drift/metered warnings, and the
// write-only OAuth token editor. Drives the real UI against a mocked auth API
// (the endpoints ship on a separate backend PR; this never touches :8420 or the
// real ~/.no_human/.env). Verifies the security properties, not just rendering.
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
await new Promise((r) => srv.listen(4670, r));

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};

const browser = await chromium.launch();
const TOKEN = "sk-ant-oat-SECRETvalue123";

// Open Settings and switch to the Account section. `authRoutes(page, opts)` must
// already be installed. Returns the page.
async function openAccount(page) {
  await page.goto("http://127.0.0.1:4670/", { waitUntil: "networkidle" });
  await page.waitForTimeout(300);
  await page.getByRole("button", { name: /^Settings$/ }).click();
  await page.waitForTimeout(300);
  await page.getByRole("button", { name: /^Account$/ }).click();
  await page.waitForTimeout(300);
}

function baseStatus(over = {}) {
  return {
    configured_profile: "personal", active_profile: "personal2",
    restart_required: true, token_var: "CLAUDE_CODE_OAUTH_TOKEN_PERSONAL",
    token_present: true,
    profiles: [{ name: "default", token_present: true }, { name: "personal", token_present: true }],
    metered_key_present: true, ...over,
  };
}

// ── Scenario 1: warnings + status + successful write (write-only proof) ──────
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  let putBody = null;
  await page.route("**/api/**", (route) => {
    const req = route.request(); const u = req.url();
    const j = (b, s = 200) => route.fulfill({ status: s, contentType: "application/json", body: JSON.stringify(b) });
    if (u.includes("/api/auth/status")) return j(baseStatus());
    if (u.includes("/api/auth/token") && req.method() === "PUT") {
      putBody = JSON.parse(req.postData() || "{}");
      // A successful save returns fresh status with the drift resolved.
      return j(baseStatus({ restart_required: false, active_profile: "personal", metered_key_present: false }));
    }
    if (u.includes("/api/onboarding")) return j({ completed: true });
    if (u.includes("/api/tasks")) return j([]);
    if (u.includes("/api/projects")) return j([]);
    return j({});
  });
  await openAccount(page);

  check("[s1] restart-required warning is shown (real alert)",
    await page.locator('.nh-alarm', { hasText: /Restart required/ }).isVisible().catch(() => false));
  check("[s1] metered-key warning is shown (real alert)",
    await page.locator('.nh-alarm', { hasText: /ANTHROPIC_API_KEY/ }).isVisible().catch(() => false));
  check("[s1] configured vs active billing profile are both surfaced",
    (await page.locator('.auth-status').innerText().catch(() => "")).match(/personal/) &&
    (await page.locator('.auth-status').innerText().catch(() => "")).includes("personal2"));

  const input = page.locator('input[aria-label="OAuth token"]');
  check("[s1] token field is write-only (type=password)", (await input.getAttribute("type")) === "password");
  check("[s1] token field disables autofill (autocomplete=off)", (await input.getAttribute("autocomplete")) === "off");

  await input.fill(TOKEN);
  await page.getByRole("button", { name: /Save token/ }).click();
  await page.waitForTimeout(400);

  check("[s1] the profile+token were PUT to the API", putBody && putBody.token === TOKEN && putBody.profile === "personal",
    JSON.stringify(putBody && { profile: putBody.profile, tokenLen: (putBody.token || "").length }));
  check("[s1] the token field is CLEARED after a successful save (write-only)",
    (await input.inputValue()) === "");
  const dom = await page.content();
  check("[s1] the token value is NOT echoed anywhere in the DOM after submit", !dom.includes(TOKEN));
  check("[s1] a 'Saved' confirmation is shown", await page.locator('.auth-saved').isVisible().catch(() => false));
  check("[s1] the restart-required warning clears after the drift is resolved",
    !(await page.locator('.nh-alarm', { hasText: /Restart required/ }).isVisible().catch(() => false)));
  check("[s1] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── Scenario 2: a metered API key is REFUSED — 422 detail rendered verbatim ──
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const DETAIL = "That is a metered API key (sk-ant-api…). no_human is OAuth-only; paste a CLAUDE_CODE_OAUTH_TOKEN instead.";
  await page.route("**/api/**", (route) => {
    const req = route.request(); const u = req.url();
    const j = (b, s = 200) => route.fulfill({ status: s, contentType: "application/json", body: JSON.stringify(b) });
    if (u.includes("/api/auth/status")) return j(baseStatus({ restart_required: false, metered_key_present: false }));
    if (u.includes("/api/auth/token") && req.method() === "PUT") return j({ detail: DETAIL }, 422);
    if (u.includes("/api/onboarding")) return j({ completed: true });
    if (u.includes("/api/tasks")) return j([]);
    if (u.includes("/api/projects")) return j([]);
    return j({});
  });
  await openAccount(page);
  const input = page.locator('input[aria-label="OAuth token"]');
  await input.fill("sk-ant-api-someapikey");
  await page.getByRole("button", { name: /Save token/ }).click();
  await page.waitForTimeout(400);
  check("[s2] the backend's refusal detail is rendered verbatim",
    (await page.locator('.settings-error').innerText().catch(() => "")).includes("OAuth-only; paste a CLAUDE_CODE_OAUTH_TOKEN"),
    await page.locator('.settings-error').innerText().catch(() => "(none)"));
  check("[s2] no 'Saved' shown on a refusal", !(await page.locator('.auth-saved').isVisible().catch(() => false)));
  check("[s2] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

// ── Scenario 3: endpoints absent (build without the auth API) — degrades ─────
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("**/api/**", (route) => {
    const u = route.request().url();
    const j = (b, s = 200) => route.fulfill({ status: s, contentType: "application/json", body: JSON.stringify(b) });
    if (u.includes("/api/auth/status")) return route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
    if (u.includes("/api/onboarding")) return j({ completed: true });
    if (u.includes("/api/tasks")) return j([]);
    if (u.includes("/api/projects")) return j([]);
    return j({});
  });
  await openAccount(page);
  check("[s3] the panel degrades to an 'unavailable' note when the endpoints are absent",
    (await page.locator('.settings-empty').innerText().catch(() => "")).match(/unavailable/i) != null);
  check("[s3] no token field is rendered when status is unavailable",
    (await page.locator('input[aria-label="OAuth token"]').count()) === 0);
  check("[s3] no page errors", errors.length === 0, errors[0] || "");
  await ctx.close();
}

await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
