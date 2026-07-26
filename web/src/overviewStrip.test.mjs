import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { overviewState } from "./overviewStrip.js";

// SCRUM-84: the header strip used to mix now-metrics ("2 need you", "oldest
// <1m") with UNLABELED all-time outcome tallies ("17 failed · 75 cancelled")
// that duplicated (and disagreed with) the sidebar's 24h-scoped ledger count.
// overviewState is the strip's one source of truth — it must report only
// live state, and the fixture payloads below exercise it directly.

const NOW = new Date("2026-07-26T12:00:00Z").getTime();
const minAgo = (m) => new Date(NOW - m * 60_000).toISOString();

function t(status, minutes, extra = {}) {
  return { status, updated_at: minAgo(minutes), ...extra };
}

test("nothing-needs-you state holds regardless of past outcomes", () => {
  const s = overviewState([
    t("done", 5),
    t("failed", 10),
    t("failed", 20, { cancelled: true }),
  ], NOW);
  assert.equal(s.allClear, true);
  assert.equal(s.needsYouCount, 0);
  assert.equal(s.oldestWaitingSec, null);
});

test("counts needs-you tasks and reports the oldest wait in seconds", () => {
  const s = overviewState([
    t("awaiting_input", 3),
    t("escalated", 45),
    t("implementing", 1), // in flight, not needs-you
  ], NOW);
  assert.equal(s.allClear, false);
  assert.equal(s.needsYouCount, 2);
  assert.equal(s.oldestWaitingSec, 45 * 60);
});

test("running/queued come from the same deriveCounts the board and sidebar use", () => {
  const s = overviewState([
    t("implementing", 1, { claimed: true }), // running
    t("implementing", 1, { claimed: false }), // queued
  ], NOW);
  assert.equal(s.running, 1);
  assert.equal(s.queued, 1);
});

test("all-time failed/cancelled tallies are not part of the strip's state at all", () => {
  const s = overviewState([
    t("failed", 1), t("failed", 2), t("failed", 3, { cancelled: true }),
  ], NOW);
  assert.deepEqual(Object.keys(s).sort(), [
    "allClear", "needsYouCount", "oldestWaitingSec", "queued", "running",
  ].sort());
});

test("empty/missing task list is handled without throwing", () => {
  assert.equal(overviewState(undefined, NOW).allClear, true);
  assert.equal(overviewState([], NOW).allClear, true);
});

// Static source check: the rendered strip must not reintroduce an unlabeled
// all-time tally, and "oldest" must carry its noun.
const here = fileURLToPath(new URL(".", import.meta.url));
const appJsx = readFileSync(here + "App.jsx", "utf8");
const overviewFn = appJsx.match(/function OverviewStrip\([^)]*\)\s*\{[^]*?\n\}/)[0];
// Strip comments before scanning for a re-introduced tally — the function's
// own explanatory comment is allowed to say "failed"/"cancelled", only the
// rendered JSX must not.
const overviewFnCode = overviewFn
  .replace(/\/\*[\s\S]*?\*\//g, "")
  .replace(/(^|[^:])\/\/[^\n]*/g, "$1");

test("the rendered strip has no bare 'failed'/'cancelled' outcome tally", () => {
  assert.doesNotMatch(overviewFnCode, /\bfailed\b/i, "OverviewStrip must not render an outcome tally itself");
  assert.doesNotMatch(overviewFnCode, /\bcancelled\b/i, "OverviewStrip must not render an outcome tally itself");
});

test("'oldest' names what it is the oldest OF", () => {
  assert.match(overviewFn, /oldest waiting/, "'oldest' must gain its noun, e.g. 'oldest waiting <1m'");
});

test("the all-clear label says what it measures, not 'all clear'", () => {
  assert.match(overviewFn, /nothing needs you/);
  assert.doesNotMatch(overviewFn, /"ov-clear">all clear/);
});
