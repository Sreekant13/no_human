import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// Task 1.5: sidebar section groups + motion pass. Like settingsOverlay.test.mjs
// and slideOverSummary.test.mjs, this is static source analysis — no
// jsdom/React renderer is wired into this project's `node --test` harness, so
// these assertions read the .jsx/.css source rather than mounting components.

const here = fileURLToPath(new URL(".", import.meta.url));
const appJsx = readFileSync(here + "App.jsx", "utf8");
const stylesCss = readFileSync(here + "styles.css", "utf8");

test("the sidebar nav is grouped under muted uppercase group headers: Work and Insights", () => {
  assert.match(appJsx, /<NavGroup\s+title=["']Work["']/, "a 'Work' group header must render");
  assert.match(appJsx, /<NavGroup\s+title=["']Insights["']/, "an 'Insights' group header must render");
  // The group component actually renders the title into the muted uppercase header class.
  assert.match(appJsx, /function NavGroup[^]*?nh-navgroup-title[^]*?\{title\}/);
});

test("Board, Backlog, Done and Failed are grouped under Work; Stats under Insights, keeping the same handlers", () => {
  // Same destinations/handlers as before — visual grouping only, no nav-model change.
  assert.match(appJsx, /setPage\(\s*["']board["']\s*\)/);
  assert.match(appJsx, /setPage\(\s*["']backlog["']\s*\)/);
  assert.match(appJsx, /setPage\(\s*["']done["']\s*\)/);
  assert.match(appJsx, /setPage\(\s*["']failed["']\s*\)/);
  assert.match(appJsx, /setPage\(\s*["']stats["']\s*\)/);
  // The old side-by-side outcome pill bar is gone — Done/Failed now live as nav rows.
  assert.doesNotMatch(appJsx, /nh-outcomes/, "the old .nh-outcomes bar must be replaced by grouped nav rows");
});

test("group headers use the muted uppercase treatment (letter-spacing + uppercase + dim/muted color)", () => {
  const m = stylesCss.match(/\.nh-navgroup-title\s*\{([^}]*)\}/);
  assert.ok(m, ".nh-navgroup-title rule must exist");
  assert.match(m[1], /text-transform:\s*uppercase/);
  assert.match(m[1], /letter-spacing/);
  assert.match(m[1], /var\(--text-dim\)|var\(--text-muted\)/);
});

test("group headers resolve to --text-muted specifically, not --text-dim (contrast-floor regression guard: --text-dim is 2.80:1 in dark theme, below the 4.5:1 brief requirement)", () => {
  const m = stylesCss.match(/\.nh-navgroup-title\s*\{([^}]*)\}/);
  assert.ok(m, ".nh-navgroup-title rule must exist");
  assert.match(m[1], /color:\s*var\(--text-muted\)/, ".nh-navgroup-title must use --text-muted (~5.41:1 dark / ~9:1 light), not --text-dim (2.80:1 dark, fails the brief's 4.5:1 floor)");
  assert.doesNotMatch(m[1], /color:\s*var\(--text-dim\)/, ".nh-navgroup-title must not regress back to --text-dim");
});

// The Backlog row lives in the Work group WITH Done and Failed, and it is a
// page like they are (not an overlay), so it must route and be announced as
// the current page the same way.
test("Backlog is a Work-group nav row that routes to its own page and is rendered", () => {
  const work = appJsx.match(/<NavGroup title="Work">[\s\S]*?<\/NavGroup>/)?.[0];
  assert.ok(work, "the Work group must be found");
  assert.match(work, /label="Backlog"/, "Backlog must sit in the Work group, beside Done and Failed");
  // Ordering: Backlog is the work that hasn't started; the two OUTCOME lists
  // follow it.
  assert.ok(work.indexOf('label="Backlog"') < work.indexOf('label="Done"'));
  assert.ok(work.indexOf('label="Done"') < work.indexOf('label="Failed"'));
  // aria-current is what marks the current PAGE for a screen reader; a row
  // that sets `active` but not `current` claims a pill without claiming a page.
  const row = work.match(/<NavRow\s+icon=\{<IconBacklog \/>\}[\s\S]*?\/>/)?.[0];
  assert.ok(row, "the Backlog NavRow must be found");
  assert.match(row, /active=\{page === "backlog"\}/);
  assert.match(row, /current=\{page === "backlog"\}/);
  // …and the page it routes to must actually render something.
  assert.match(appJsx, /\{page === "backlog" && \(\s*\n\s*<Backlog/);
  assert.match(appJsx, /page === "backlog" \? "Backlog"/, "the sr-only h1 must name the page");
});

test("every sidebar nav row (Board/Backlog/Done/Failed/Stats/Settings) pairs an inline-SVG icon with a text label — never icon-only", () => {
  const rowFn = appJsx.match(/function NavRow\([^)]*\)\s*\{[^]*?\n\}/);
  assert.ok(rowFn, "a shared NavRow component must exist");
  assert.match(rowFn[0], /nh-navrow-icon["'][^]*?\{icon\}/, "NavRow must render the icon prop inside an icon wrapper");
  assert.match(rowFn[0], /nh-navrow-label["'][^]*?\{label\}/, "NavRow must render the label prop as text");
  // Guard against an icon-only render path: the label span must not be conditionally
  // suppressed (no `label &&` / `showLabel` gate around nh-navrow-label).
  assert.doesNotMatch(rowFn[0], /\{[^}]*label\s*&&[^}]*\}\s*<span className="nh-navrow-label"/);
  // Every icon component used by a NavRow renders a real inline <svg> (CSP forbids
  // remote icon fonts/images).
  for (const iconFn of ["IconBoard", "IconDone", "IconFailed", "IconStats", "IconGear"]) {
    const m = appJsx.match(new RegExp(`function ${iconFn}\\([^)]*\\)\\s*\\{[^]*?\\n\\}`));
    assert.ok(m, `${iconFn} must be defined`);
    assert.match(m[0], /<svg/i, `${iconFn} must render an inline <svg>`);
    assert.match(appJsx, new RegExp(`icon=\\{<${iconFn}\\s*/>\\}`), `a NavRow must be given ${iconFn} as its icon`);
  }
});

test("no emoji used as an icon in the sidebar nav rows", () => {
  // A rough but effective guard: emoji code points in the nav row / icon region.
  const navRegion = appJsx.slice(appJsx.indexOf("function NavIcon"), appJsx.indexOf("export default function App"));
  // eslint-disable-next-line no-misleading-character-class
  assert.doesNotMatch(navRegion, /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/u, "no emoji glyphs in the nav icon set");
});

test("the active nav row gets a soft filled pill with an animated, reduced-motion-guarded transition", () => {
  assert.match(stylesCss, /\.nh-navrow\.active\s*\{[^}]*background:\s*var\(--accent-100\)/s);
  const rowRule = stylesCss.match(/\.nh-navrow\s*\{([^}]*)\}/);
  assert.ok(rowRule, ".nh-navrow base rule must exist");
  assert.match(rowRule[1], /transition:[^;]*background[^;]*var\(--dur-fast\)/, "active-state transition must use --dur-fast");
});

test("the Settings gear is pinned at the bottom of the sidebar and opens the existing settings overlay", () => {
  const footIdx = appJsx.indexOf('className="nh-sidebar-foot"');
  const settingsBtnIdx = appJsx.indexOf("nh-settings-row");
  assert.ok(footIdx > -1 && settingsBtnIdx > footIdx, "the Settings row must live inside/after nh-sidebar-foot");
  assert.match(appJsx, /setSettingsOpen\(true\)/);
  // It's the last interactive row rendered inside the sidebar (bottom-pinned).
  const asideClose = appJsx.indexOf("</aside>");
  const lastSettingsMention = appJsx.lastIndexOf("nh-settings-row");
  const lastNavgroupMention = appJsx.lastIndexOf("nh-navgroup");
  assert.ok(lastSettingsMention > lastNavgroupMention, "Settings row must render after the nav groups");
  assert.ok(lastSettingsMention < asideClose);
});

test("--sp scale tokens are reused (not redefined) and applied in the touched sidebar/ledger/overview regions", () => {
  // Exactly one definition site for each token (in :root), task 1.4's scale — 1.5 must not redefine it.
  for (const n of [1, 2, 3, 4, 5, 6, 7, 8]) {
    const defs = [...stylesCss.matchAll(new RegExp(`--sp-${n}\\s*:\\s*\\d+px`, "g"))];
    assert.equal(defs.length, 1, `--sp-${n} must be defined exactly once (reused, not redefined)`);
  }
  // The touched regions (sidebar, ledger, overview strip) actually use var(--sp-*).
  const sidebarBlock = stylesCss.slice(stylesCss.indexOf(".nh-sidebar {"), stylesCss.indexOf("/* Mobile: the sidebar becomes a horizontal top bar"));
  assert.match(sidebarBlock, /var\(--sp-\d\)/, "the sidebar block must use the --sp scale");
  const ledgerBlock = stylesCss.slice(stylesCss.indexOf("\n.nh-ledger {"), stylesCss.indexOf(".nh-ledger + .nh-sidebar-foot"));
  assert.match(ledgerBlock, /var\(--sp-\d\)/, "the night ledger block must use the --sp scale");
  const overviewBlock = stylesCss.match(/(?:^|\n)\.nh-overview\s*\{([^}]*)\}/)[1];
  assert.match(overviewBlock, /var\(--sp-\d\)/, "the overview strip block must use the --sp scale");
});

test("board card entrance is a translateY(4px)+fade over --dur-fast, staggered 30-40ms/card and capped", () => {
  const kf = stylesCss.match(/@keyframes nh-card-arrive\s*\{([^]*?)\n\}/);
  assert.ok(kf, "nh-card-arrive keyframes must exist");
  assert.match(kf[1], /translateY\(4px\)/, "card enter must translate 4px (no layout-shifting properties)");
  assert.doesNotMatch(kf[1], /width|height|top:|left:/, "card enter must not animate layout-affecting properties");
  const rule = stylesCss.match(/\.task-card\s*\{\s*animation:\s*nh-card-arrive[^}]*\}/);
  assert.ok(rule, ".task-card must use the nh-card-arrive animation");
  assert.match(rule[0], /var\(--dur-fast\)/, "card enter duration must be --dur-fast");
  // Stagger: consecutive per-card delays 30-40ms apart, with a cap for large lanes.
  const delays = [...stylesCss.matchAll(/\.task-card:nth-child\((\d+)\)\s*\{\s*animation-delay:\s*([\d.]+)s/g)]
    .map(([, n, d]) => [Number(n), Number(d) * 1000]);
  assert.ok(delays.length >= 3, "at least a few explicit per-card stagger delays must be defined");
  for (let i = 1; i < delays.length; i++) {
    const step = delays[i][1] - delays[i - 1][1];
    assert.ok(step >= 30 && step <= 40, `stagger step between card ${delays[i - 1][0]} and ${delays[i][0]} must be 30-40ms, was ${step}ms`);
  }
  assert.match(stylesCss, /:nth-child\(n\+\d+\)\s*\{\s*animation-delay:/, "the stagger must be capped for lanes beyond a few cards");
});

test("the card's active-status pulse has its own non-colliding keyframe name (no more @keyframes pulse-dot collision)", () => {
  assert.doesNotMatch(stylesCss, /@keyframes pulse-dot\b/, "the old colliding 'pulse-dot' name (defined twice, silently overriding) must be gone");
  assert.match(stylesCss, /@keyframes card-status-pulse\b/, "the card's active pulse must have its own keyframe name");
  assert.match(stylesCss, /animation:\s*card-status-pulse/);
  // No @keyframes name is defined more than once anywhere in the file — a duplicate
  // silently overrides every usage of the earlier definition (the exact bug fixed here).
  const names = [...stylesCss.matchAll(/@keyframes\s+([\w-]+)\s*\{/g)].map((m) => m[1]);
  const counts = {};
  for (const n of names) counts[n] = (counts[n] || 0) + 1;
  const dupes = Object.entries(counts).filter(([, c]) => c > 1).map(([n]) => n);
  assert.deepEqual(dupes, [], `duplicate @keyframes names collide: ${dupes.join(", ")}`);
});

test("board/sidebar motion is prefers-reduced-motion guarded", () => {
  const guards = [...stylesCss.matchAll(/@media \(prefers-reduced-motion: reduce\)\s*\{([^]*?)\n\}/g)]
    .map((m) => m[1]).join("\n");
  assert.match(guards, /nh-navrow/, "the nav active-pill transition must have a reduced-motion override");
});
