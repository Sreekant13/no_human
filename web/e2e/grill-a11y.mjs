// a11y guard for the intake/grill modals. These render as bare divs; this makes
// them proper dialogs (role + accessible name + focus moved in + Tab trap). The
// test forces the SSE endpoint to fail so the grill falls back to POST /api/grill,
// returns a question step, and asserts the resulting modal is a real dialog whose
// focus is trapped. Mocked API, no :8420.
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
await new Promise((r) => srv.listen(4660, r));

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};
// The exact focusable set the app's trap uses, evaluated in-page.
const FOCUSABLE_EVAL = `(() => {
  const el = document.querySelector('.new-task-modal[role="dialog"]');
  if (!el) return null;
  const f = [...el.querySelectorAll('button,[href],input,select,textarea,[tabindex]:not([tabindex=\\"-1\\"])')]
    .filter((n) => !n.disabled && n.getClientRects().length > 0);
  return { el, f };
})()`;

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
await page.route("**/api/**", (route) => {
  const u = route.request().url();
  const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
  // Force the streaming grill to fail so the code falls back to POST /api/grill.
  if (u.includes("/api/grill/stream")) {
    return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "no stream in test" }) });
  }
  if (u.includes("/api/grill")) {
    return j({ type: "question", question: "Which datastore should the endpoint read from?", suggestions: ["A: primary", "B: replica"], round: 1 });
  }
  if (u.includes("/api/onboarding")) return j({ completed: true });
  if (u.includes("/api/tasks")) return j([]);
  if (u.includes("/api/projects")) return j([]);
  return j({});
});
await page.goto("http://127.0.0.1:4660/", { waitUntil: "networkidle" });
await page.waitForTimeout(400);

// Open the composer, fill prompt + repo, start the grill.
await page.getByRole("button", { name: /\+ New Task/ }).click();
await page.waitForTimeout(300);
await page.getByPlaceholder(/Describe the task/i).fill("Add analytics export endpoint");
await page.getByLabel("Repository path").fill("~/git/metrics-core");
await page.getByRole("button", { name: /Next/ }).click();

// Wait for the QUESTION modal specifically (loading -> SSE 500 -> POST fallback
// -> question). Keying on the answer textarea guarantees we're past the loading
// state, so the trap is exercised against MULTIPLE focusables (not the loading
// modal's single Cancel button, where a wrap would pass trivially).
const dialog = page.locator('.new-task-modal[role="dialog"]');
await page.getByPlaceholder(/Your answer/i).waitFor({ state: "visible", timeout: 6000 }).catch(() => {});
check("intake grill QUESTION modal is a dialog", await dialog.count() >= 1);
const name = await dialog.getAttribute("aria-label").catch(() => null);
check("the dialog has an accessible name", name === "Intake grill", `aria-label=${name}`);
check("aria-modal is set", (await dialog.getAttribute("aria-modal").catch(() => null)) === "true");

// Focus was pulled into the dialog on open.
const nFocus = await page.evaluate(`(() => { const r = ${FOCUSABLE_EVAL}; return r ? r.f.length : 0; })()`);
// The trap is only meaningfully exercised with >1 focusable; guard against
// accidentally testing a single-control modal where any wrap passes trivially.
check("the question modal has multiple focusables (trap is non-trivial)", nFocus > 1, `${nFocus} focusables`);
check("focus is inside the dialog after it opens",
  await page.evaluate(() => {
    const el = document.querySelector('.new-task-modal[role="dialog"]');
    return !!el && el.contains(document.activeElement);
  }));

// Tab from the LAST focusable must wrap to the first — i.e. stay in the dialog.
await page.evaluate(`(() => { const r = ${FOCUSABLE_EVAL}; if (r && r.f.length) r.f[r.f.length - 1].focus(); })()`);
await page.keyboard.press("Tab");
const afterTab = await page.evaluate(() => {
  const el = document.querySelector('.new-task-modal[role="dialog"]');
  const f = [...el.querySelectorAll('button,[href],input,select,textarea,[tabindex]:not([tabindex="-1"])')]
    .filter((n) => !n.disabled && n.getClientRects().length > 0);
  return { inDialog: el.contains(document.activeElement), isFirst: document.activeElement === f[0] };
});
check("Tab at the end wraps back into the dialog (trap holds)",
  afterTab.inDialog && afterTab.isFirst, JSON.stringify(afterTab));

// Shift+Tab from the FIRST must wrap to the last.
await page.evaluate(`(() => { const r = ${FOCUSABLE_EVAL}; if (r && r.f.length) r.f[0].focus(); })()`);
await page.keyboard.press("Shift+Tab");
const afterShift = await page.evaluate(() => {
  const el = document.querySelector('.new-task-modal[role="dialog"]');
  const f = [...el.querySelectorAll('button,[href],input,select,textarea,[tabindex]:not([tabindex="-1"])')]
    .filter((n) => !n.disabled && n.getClientRects().length > 0);
  return { inDialog: el.contains(document.activeElement), isLast: document.activeElement === f[f.length - 1] };
});
check("Shift+Tab at the start wraps to the end (trap holds)",
  afterShift.inDialog && afterShift.isLast, JSON.stringify(afterShift));

// H3: the grill overlays had ZERO coverage — restoring `onClick={onClose}` on
// them left this suite green.
// ⚠️ SCOPE: this suite drives the three `aria-label="Intake grill"` branches.
// The fourth overlay — `aria-label="Refined spec"`, the one the operator sees
// when the grill finishes — is reached by NO suite, so its backdrop and caret
// behaviour are still unpinned. Covering it needs a harness that drives the
// grill to completion; recorded rather than silently left as "H3 closed". A stray click mid-grill would discard every
// answer the operator had already given, which is the same class of loss the
// composer and reply modal were fixed for.
{
  const dialog = page.locator('.new-task-modal[role="dialog"]');
  await page.mouse.click(6, 6);                 // the backdrop, well outside
  await page.waitForTimeout(300);
  check("[backdrop] a stray click does not discard the intake grill",
    await dialog.isVisible().catch(() => false));
  // …and it must not strand the caret either: most of this dialog is static
  // spec text, and a mousedown on it used to drop every following keystroke.
  const focusable = await page.evaluate(() => {
    const el = document.querySelector('.new-task-modal[role="dialog"]');
    const t = el && el.querySelector("input, textarea");
    if (t) { t.focus(); return true; }
    return false;
  });
  if (focusable) {
    await page.locator('.new-task-modal .sendback-label').first().click();
    await page.waitForTimeout(150);
    const held = await page.evaluate(() => document.activeElement?.tagName || "none");
    check("[caret] clicking the spec text does not strand the caret",
      held === "INPUT" || held === "TEXTAREA", held);
  }
}

check("no page errors", errors.length === 0, errors[0] || "");

await ctx.close();
await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
