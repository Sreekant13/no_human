// Deterministic, server-free layout regression for the test-plan section header.
// Guards the fix in T1: the "+ Add layer" control must NOT stretch full-width
// (the global `.btn { flex: 1 }` used to grow it), and the "Test Plan" label
// must not be clipped or jammed against it. Renders the exact Settings.jsx
// markup (project-expanded-body > memory-header > ntm-label + btn) against the
// real styles.css in headless Chromium and measures computed layout.
import { chromium } from "playwright";
import { readFileSync } from "node:fs";

const css = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

// Mirror of Settings.jsx:615-620 (the test-plan section header).
const html = `<!doctype html><html><head><style>
  :root { color-scheme: light; }
  body { margin: 0; }
  #card { width: 760px; padding: 0 16px; }
  ${css}
</style></head><body>
  <div id="card">
    <div class="project-expanded-body">
      <div class="memory-header" style="margin-bottom:8px">
        <span class="ntm-label" style="margin-bottom:0">Test Plan</span>
        <button class="btn btn-sendback btn-sm">+ Add layer</button>
      </div>
    </div>
  </div>
</body></html>`;

const b = await chromium.launch();
const page = await b.newPage({ viewport: { width: 1000, height: 400 } });
await page.setContent(html, { waitUntil: "load" });

const m = await page.evaluate(() => {
  const row = document.querySelector(".project-expanded-body > .memory-header");
  const label = row.querySelector(".ntm-label");
  const btn = row.querySelector(".btn");
  const r = row.getBoundingClientRect();
  const lb = label.getBoundingClientRect();
  const bb = btn.getBoundingClientRect();
  return {
    rowWidth: r.width,
    btnWidth: bb.width,
    labelClipped: label.scrollWidth > label.clientWidth + 1,
    gap: bb.left - lb.right, // horizontal separation between label and button
  };
});
await b.close();

const fails = [];
// 1. The add-layer button must be content-sized, not full-width.
if (m.btnWidth > m.rowWidth * 0.5)
  fails.push(`add-layer button is full-width: ${m.btnWidth.toFixed(0)}px of ${m.rowWidth.toFixed(0)}px row (expected < 50%)`);
// 2. The label text must not be clipped.
if (m.labelClipped)
  fails.push("Test Plan label is clipped (scrollWidth > clientWidth)");
// 3. There must be visible separation between the label and the control.
if (m.gap < 8)
  fails.push(`label and button too close: gap ${m.gap.toFixed(0)}px (expected >= 8px)`);

if (fails.length) {
  console.error("FAIL test-plan-header layout:\n  - " + fails.join("\n  - "));
  console.error("measured:", JSON.stringify(m));
  process.exit(1);
}
console.log("PASS test-plan-header layout", JSON.stringify(m));
