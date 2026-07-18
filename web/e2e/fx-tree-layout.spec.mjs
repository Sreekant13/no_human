// Deterministic, server-free layout regression for the SYSTEM tab agent tree.
// The pipeline board caps at ~980px and packs 5 stage columns (~180px each),
// so a long subagent name ("Find guard.py tool allow-list seam") used to
// overflow its column and collide with the neighbour stage's rows. Renders the
// real .fx-* markup against the real styles.css and asserts the child row's
// name truncates INSIDE its column instead of spilling into the next one.
import { chromium } from "playwright";
import { readFileSync } from "node:fs";

const css = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

const laneChild = (stage, long) => `
  <div class="fx-lane s-done">
    <div class="fx-head"><div class="fx-head-top"><span class="fx-label">${stage}</span></div>
      <div class="fx-head-meta"><span class="fx-status s-done">done</span></div></div>
    <div class="fx-role-list">
      <button class="fx-role-row s-done"><span class="fx-role-icon">◈</span>
        <span class="fx-role-name">${stage}</span><span class="fx-role-dot s-done"></span>
        <span class="fx-role-chev">›</span></button>
      <div class="fx-children">
        <div class="fx-node fx-node--child is-last">
          <button class="fx-role-row s-done"><span class="fx-role-icon">◇</span>
            <span class="fx-role-name" id="target">${long}</span>
            <span class="fx-role-dot s-done"></span><span class="fx-role-chev">›</span></button>
        </div>
      </div>
    </div>
  </div>`;

const html = `<!doctype html><html><head><style>
  body { margin: 0; width: 980px; } .fx-board { padding: 0; }
  ${css}
</style></head><body>
  <div class="fx-board">
    ${laneChild("Planning", "Find guard.py tool allow-list seam")}
    ${laneChild("Coding", "Coder")}
    ${laneChild("Reviewing", "Reviewer")}
    ${laneChild("Shepherding", "Watcher")}
    ${laneChild("Orchestrating", "Orchestrator")}
  </div>
</body></html>`;

const b = await chromium.launch();
const page = await b.newPage({ viewport: { width: 1100, height: 700 } });
await page.setContent(html, { waitUntil: "load" });

const m = await page.evaluate(() => {
  const name = document.getElementById("target");
  const lane = name.closest(".fx-lane");
  const nb = name.getBoundingClientRect();
  const lb = lane.getBoundingClientRect();
  return {
    laneRight: lb.right,
    nameRight: nb.right,
    truncated: name.scrollWidth > name.clientWidth + 1,
    overflow: nb.right - lb.right,
  };
});

// The expanded agent's log pane lives below the board inside `.sys-tree`
// (align-items:center). Without an explicit width it collapses to max-content —
// a narrow centered sliver on a SHORT log. Guard that it spans the board width.
const panelPage = await b.newPage({ viewport: { width: 1100, height: 700 } });
await panelPage.setContent(`<!doctype html><html><head><style>
  body{margin:0;width:1080px;} ${css}
</style></head><body><div class="sys-view"><div class="sys-tree">
  <div class="fx-board"><div class="fx-lane s-done"><div class="fx-head"><div class="fx-head-top"><span class="fx-label">Reviewing</span></div></div></div></div>
  <div class="fx-log-panel" style="--node-color: var(--accent-500)">
    <div class="fx-log-head"><span class="fx-log-icon">✓</span>
      <div class="fx-log-titles"><div class="fx-log-type">GATE</div><div class="fx-log-name">Reviewer</div></div></div>
    <div class="fx-log-events"><div class="activity-event-rich role-reviewer"><div class="rich-body">PASS</div></div></div>
  </div>
</div></div></body></html>`, { waitUntil: "load" });
const p = await panelPage.evaluate(() => {
  const panel = document.querySelector(".fx-log-panel");
  const board = document.querySelector(".fx-board");
  const pb = panel.getBoundingClientRect(), bb = board.getBoundingClientRect();
  return { panelW: pb.width, boardW: bb.width, panelLeft: pb.left, boardLeft: bb.left };
});
await b.close();

const fails = [];
// The long name's right edge must stay within its own stage column.
if (m.nameRight > m.laneRight + 1)
  fails.push(`child name overflows its column by ${m.overflow.toFixed(0)}px (nameRight ${m.nameRight.toFixed(0)} > laneRight ${m.laneRight.toFixed(0)})`);
// A name too long for the column must be ellipsis-truncated, not clipped raw.
if (!m.truncated)
  fails.push("long child name is not truncated (no ellipsis) — it should be");
// A short log must still fill the board width, not collapse to a centered sliver.
if (Math.abs(p.panelW - p.boardW) > 2 || Math.abs(p.panelLeft - p.boardLeft) > 2)
  fails.push(`log panel does not span the board on a short log: panel ${p.panelW.toFixed(0)}px@${p.panelLeft.toFixed(0)} vs board ${p.boardW.toFixed(0)}px@${p.boardLeft.toFixed(0)}`);

if (fails.length) {
  console.error("FAIL fx-tree-layout:\n  - " + fails.join("\n  - "));
  console.error("measured:", JSON.stringify(m));
  process.exit(1);
}
console.log("PASS fx-tree-layout", JSON.stringify(m));
