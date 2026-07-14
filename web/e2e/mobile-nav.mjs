// Regression guard: every top-level nav control must be REACHABLE on a phone.
// Playwright's .click() scrolls a target into view programmatically, so a click
// test alone passes even when the button renders off-screen — this asserts on
// geometry (rect.right <= innerWidth), which is what a human's thumb sees.
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
await new Promise((r) => srv.listen(4620, r));

const failures = [];
const check = (n, ok, d = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${n}${d ? "  — " + d : ""}`);
  if (!ok) failures.push(n);
};

const browser = await chromium.launch();

for (const [label, viewport] of [
  ["mobile", { width: 390, height: 844 }],
  ["small", { width: 360, height: 780 }],
  ["desktop", { width: 1440, height: 900 }],
]) {
  for (const theme of ["dark", "light"]) {
    const ctx = await browser.newContext({ viewport });
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.addInitScript((t) => localStorage.setItem("nh-theme", t), theme);
    await page.route("**/api/**", (route) => {
      const u = route.request().url();
      const j = (b) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
      if (u.includes("/api/onboarding")) return j({ completed: true });
      if (u.includes("/api/tasks")) return j([]);
      if (u.includes("/api/projects")) return j([]);
      return j({});
    });
    await page.goto("http://127.0.0.1:4620/", { waitUntil: "networkidle" });
    await page.waitForTimeout(500);

    const geom = await page.evaluate(() => {
      const vw = window.innerWidth;
      const btns = [...document.querySelectorAll(".nh-sidenav-btn")].map((b) => {
        const r = b.getBoundingClientRect();
        return { label: b.textContent.trim().slice(0, 10), left: Math.round(r.left), right: Math.round(r.right), onScreen: r.left >= 0 && r.right <= vw };
      });
      const toggle = document.querySelector(".nh-theme-toggle");
      const tr = toggle?.getBoundingClientRect();
      const tag = document.querySelector(".legion-tag");
      return {
        vw,
        btns,
        toggleOnScreen: tr ? tr.right <= vw && tr.left >= 0 : null,
        tagVisible: tag ? getComputedStyle(tag).display !== "none" : false,
        pageOverflowX: document.documentElement.scrollWidth > vw + 1,
      };
    });

    const off = geom.btns.filter((b) => !b.onScreen);
    check(
      `[${label}/${theme}] every nav item is on screen`,
      off.length === 0,
      off.length ? off.map((b) => `${b.label}@${b.left}-${b.right} of ${geom.vw}`).join(", ") : `${geom.btns.length} items`,
    );
    check(`[${label}/${theme}] theme toggle is on screen`, geom.toggleOnScreen !== false);
    check(`[${label}/${theme}] no horizontal page overflow`, !geom.pageOverflowX);

    // The tagline is decorative and may be dropped on a phone — but it must SURVIVE
    // on desktop, where the sidebar is a vertical rail with room for it.
    if (label === "desktop") {
      check(`[${label}/${theme}] brand tagline still shown on desktop`, geom.tagVisible);
    }

    // And Settings must actually open.
    await page.getByRole("button", { name: /^Settings$/ }).click();
    await page.waitForTimeout(600);
    const onSettings = await page.getByRole("button", { name: /NEW PROJECT/i }).isVisible().catch(() => false);
    check(`[${label}/${theme}] Settings page opens`, onSettings);
    check(`[${label}/${theme}] no page errors`, errors.length === 0, errors[0] || "");

    await ctx.close();
  }
}

await browser.close();
srv.close();
console.log(failures.length ? `\n${failures.length} FAILURE(S)` : "\nALL CHECKS PASSED");
process.exit(failures.length ? 1 : 0);
