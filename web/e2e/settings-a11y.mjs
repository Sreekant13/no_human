// a11y guard for the Settings test-layer form's <select> controls.
// runner/gating/ci-backend selects had no accessible name (no <label>, no
// aria-label, and a <select> can't use placeholder), so a screen reader
// announced bare "combo box". This drives the real Settings UI and asserts each
// select is reachable by its accessible name. Mocked API, no :8420.
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
await new Promise((r) => srv.listen(4650, r));

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
const PROJECT = { id: "p1", name: "metrics-core", repo_paths: ["~/git/metrics-core"], test_layers: [] };
await page.route("**/api/**", (route) => {
  const u = route.request().url();
  const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
  if (u.includes("/api/onboarding")) return j({ completed: true });
  if (u.includes("/api/tasks")) return j([]);
  if (u.includes("/api/projects")) return j([PROJECT]);
  return j({});
});
await page.goto("http://127.0.0.1:4650/", { waitUntil: "networkidle" });
await page.waitForTimeout(400);

// Open Settings (opens on the Projects section by default).
await page.getByRole("button", { name: /^Settings$/ }).click();
await page.waitForTimeout(500);
check("Settings opened on Projects", await page.getByText("metrics-core").first().isVisible().catch(() => false));

// Expand the project card, then open the add-layer form.
await page.getByText("metrics-core").first().click();
await page.waitForTimeout(300);
const addLayer = page.getByRole("button", { name: /Add layer/i });
check("project expands to reveal the test-layer editor", await addLayer.isVisible().catch(() => false));
await addLayer.click();
await page.waitForTimeout(300);

// runner + gating selects are shown for the default (local) runner.
check("Test runner select has an accessible name",
  await page.getByLabel("Test runner").isVisible().catch(() => false));
check("Gating select has an accessible name",
  await page.getByLabel("Gating").isVisible().catch(() => false));

// Switching the runner to "ci" reveals the CI backend select.
await page.getByLabel("Test runner").selectOption("ci").catch(() => {});
await page.waitForTimeout(200);
check("CI backend select has an accessible name",
  await page.getByLabel("CI backend").isVisible().catch(() => false));

// A stray click must not discard typed work. Settings has THREE overlays and all
// three closed on a backdrop click: the overlay itself, Add rule/skill, and New
// Project. One click at (6,6) destroyed two layers and everything typed in them
// — the same defect that was fixed in the composer and the reply modal, left
// open in the file that change edited.
{
  await page.keyboard.press("Escape");
  await page.waitForTimeout(200);
  await page.getByRole("button", { name: /^settings$/i }).first().click();
  await page.waitForTimeout(400);
  const newProject = page.getByRole("button", { name: /new project/i }).first();
  if (await newProject.isVisible().catch(() => false)) {
    await newProject.click();
    await page.waitForTimeout(300);
    const field = page.locator(".new-task-modal input").first();
    await field.fill("my-precious-project-name");
    await page.mouse.click(6, 6);
    await page.waitForTimeout(400);
    const state = await page.evaluate(() => ({
      settings: document.querySelectorAll(".settings-overlay").length,
      nested: document.querySelectorAll("[data-nested-modal]").length,
    }));
    check("[backdrop] a stray click does not close Settings", state.settings === 1,
      JSON.stringify(state));
    check("[backdrop] a stray click does not discard the nested modal",
      state.nested === 1, JSON.stringify(state));
    check("[backdrop] the typed project name survives the stray click",
      (await field.inputValue().catch(() => "(gone)")) === "my-precious-project-name");
    // …and again ON THE NESTED SCRIM ITSELF. A click at (6,6) cannot reach it:
    // .sendback-overlay is position:fixed inside .settings-overlay, whose
    // backdrop-filter creates a containing block, so the nested scrim covers only
    // the panel. Without this second click the nested overlay's own close is
    // completely ungated — verified: reverting it left this suite green.
    await page.evaluate(() => {
      const scrim = document.querySelector("[data-nested-modal]");
      if (scrim) scrim.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await page.waitForTimeout(300);
    const after = await page.evaluate(() => ({
      settings: document.querySelectorAll(".settings-overlay").length,
      nested: document.querySelectorAll("[data-nested-modal]").length,
    }));
    check("[backdrop] a click on the nested scrim does not discard it either",
      after.nested === 1 && after.settings === 1, JSON.stringify(after));
    check("[backdrop] the typed name survives that click too",
      (await field.inputValue().catch(() => "(gone)")) === "my-precious-project-name");
  } else {
    check("[backdrop] New Project button reachable", false, "not visible");
  }
}

// H4: the Settings focus-heal was 25 lines of untested code — deleting the whole
// MutationObserver left this suite green. And Escape was dead in New Project
// while its sibling modal had it, so with backdrop-close gone Cancel was the
// only exit, contradicting the code's own "Escape and Cancel are the exits".
{
  const nested = page.locator("[data-nested-modal]");
  if (await nested.count() > 0) {
    await page.keyboard.press("Escape");
    await page.waitForTimeout(300);
    const st = await page.evaluate(() => ({
      settings: document.querySelectorAll(".settings-overlay").length,
      nested: document.querySelectorAll("[data-nested-modal]").length,
    }));
    check("[exits] Escape closes the New Project modal", st.nested === 0,
      JSON.stringify(st));
    // …and ONLY it. Escape closing both layers would discard the Settings
    // context too — the overlay's own handler stands down for a nested modal
    // precisely so one keystroke cannot tear down two layers.
    check("[exits] …without also closing Settings underneath",
      st.settings === 1, JSON.stringify(st));
  }
  // Re-open, put focus inside, then REMOVE the focused control: the one case the
  // heal exists for. Focus must land back inside the overlay, not on <body>,
  // from where the next Tab walks into the page behind an aria-modal dialog.
  const newProject = page.getByRole("button", { name: /new project/i }).first();
  if (await newProject.isVisible().catch(() => false)) {
    await newProject.click();
    await page.waitForTimeout(300);
    await page.locator(".new-task-modal input").first().click();
    await page.evaluate(() => document.querySelector(".new-task-modal input")?.remove());
    await page.waitForTimeout(300);
    const healed = await page.evaluate(() => {
      const ov = document.querySelector(".settings-overlay");
      return Boolean(ov && ov.contains(document.activeElement));
    });
    check("[focus-trap] focus returns inside Settings when the focused control unmounts",
      healed, await page.evaluate(() => document.activeElement?.tagName || "none"));
    // …and it must NOT fire while the remembered control is still there.
    await page.evaluate(() => document.activeElement?.blur());
    await page.evaluate(() => {
      const host = document.querySelector(".settings-overlay");
      const probe = document.createElement("span");
      host.appendChild(probe); probe.remove();
    });
    await page.waitForTimeout(200);
    const stolen = await page.evaluate(() =>
      String(document.activeElement?.className || ""));
    check("[focus-trap] Settings does not heal while the remembered control exists",
      !stolen.includes("settings-overlay-close"), stolen || "BODY");
  }
}

check("no page errors", errors.length === 0, errors[0] || "");

await ctx.close();
await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
