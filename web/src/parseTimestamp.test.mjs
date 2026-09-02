import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { parseTimestamp, timestampMs } from "./parseTimestamp.js";

const HERE = dirname(fileURLToPath(import.meta.url));

test("a naive DB timestamp parses as UTC, not local", () => {
  // Asserted against Date.UTC, not against `new Date(naive)` — a genuine
  // TZ-independent assertion. Under the pre-fix code (`new Date(naive)`,
  // which reads a zone-free string as LOCAL time) this fails by exactly the
  // viewer's UTC offset when run with TZ=Asia/Jerusalem.
  const got = parseTimestamp("2026-09-01 12:00:00").getTime();
  assert.equal(got, Date.UTC(2026, 8, 1, 12, 0, 0));
  assert.equal(got, new Date("2026-09-01T12:00:00Z").getTime());
});

test("the 40-minute-old task reads 40m, not 3h", () => {
  const nowMs = Date.UTC(2026, 8, 1, 12, 0, 0);
  const fortyMinAgoMs = nowMs - 40 * 60 * 1000;
  const d = new Date(fortyMinAgoMs);
  const naive = `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}-${String(d.getUTCDate()).padStart(2, "0")} ${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}:${String(d.getUTCSeconds()).padStart(2, "0")}`;
  const ageSec = (nowMs - timestampMs(naive)) / 1000;
  assert.ok(Math.abs(ageSec - 2400) < 1, `expected ~2400s, got ${ageSec}s`);
  assert.ok(ageSec < 3600, "must read as minutes-ago, not hours-ago");
});

test("fractional-second and T-separated naive forms are UTC too", () => {
  const expected = Date.UTC(2026, 8, 1, 12, 0, 0);
  assert.equal(parseTimestamp("2026-09-01 12:00:00.123456").getTime(), expected + 123);
  assert.equal(parseTimestamp("2026-09-01T12:00:00").getTime(), expected);
});

test("offset-bearing ISO is passed through byte-identically", () => {
  for (const s of [
    "2026-09-01T12:00:00+00:00",
    "2026-09-01T12:00:00Z",
    "2026-09-01T12:00:00+03:00",
    "2026-09-01T12:00:00-05:00",
  ]) {
    assert.equal(timestampMs(s), new Date(s).getTime(), s);
  }
});

test("garbage warns once and degrades to null/NaN", () => {
  const calls = [];
  const original = console.warn;
  console.warn = (...args) => calls.push(args);
  try {
    assert.equal(parseTimestamp("not a date"), null);
    assert.ok(Number.isNaN(timestampMs("not a date")));
    assert.equal(calls.length, 1, "the second identical bad value must be deduped");

    assert.equal(parseTimestamp(null), null);
    assert.equal(parseTimestamp(""), null);
    assert.equal(parseTimestamp(undefined), null);
    assert.equal(calls.length, 1, "missing values must never warn");
    assert.doesNotThrow(() => parseTimestamp("also not a date"));
  } finally {
    console.warn = original;
  }
});

// Every module the incident audit touched (or ruled out) for a raw timestamp
// parse. `web/src/SlideOver.jsx:2172` is deliberately NOT in this list — its
// input is a numeric unix epoch (`new Date(epoch * 1000)`), which has no
// zone ambiguity and was audited-and-excluded per the plan.
const AUDITED_MODULES = [
  "Stats.jsx",
  "TaskTable.jsx",
  "Board.jsx",
  "slideOverSummary.js",
  "overviewStrip.js",
  "nightLedger.js",
  "answerLane.js",
  "App.jsx",
  "drainChip.js",
  "learningRetire.js",
  "jiraImport.js",
  "backlogSources.js",
];

// Allowlisted zone-free forms: no timestamp *string* reaches `new Date`/
// `Date.parse` directly — only `Date.now()`, arithmetic on milliseconds, or
// a numeric epoch.
const ALLOWLIST = [
  /new Date\(\)/,
  /Date\.now\(\)/,
  /new Date\(\s*now\.getTime\(\)/,
  /new Date\(\s*Math\.min\(/,
  /new Date\(\s*epoch\s*\*\s*1000\s*\)/,
];

/** Strip `//` line comments and `/* *\/` block comments so prose mentioning
 * `new Date(...)` (e.g. a doc comment explaining the old bug) is not
 * mistaken for a live call site. Safe here: none of the audited files put a
 * `//` inside a string literal (verified: no `http://`/`https://` in any of
 * them). */
function stripComments(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .split("\n")
    .map((line) => {
      const i = line.indexOf("//");
      return i === -1 ? line : line.slice(0, i);
    })
    .join("\n");
}

function rawParseSites(src) {
  const code = stripComments(src);
  const matches = code.match(/new Date\([^)]*\)|Date\.parse\([^)]*\)/g) || [];
  return matches.filter((m) => !ALLOWLIST.some((re) => re.test(m)));
}

test("no audited module re-introduces a raw timestamp parse", () => {
  // Positive control FIRST: a typo'd/overly-narrow matcher must still catch
  // an obvious violation, or a clean report here is meaningless.
  const control = rawParseSites('function f(x) { return Date.parse(x.updated); }');
  assert.deepEqual(control, ["Date.parse(x.updated)"], "positive control must be flagged");
  assert.deepEqual(rawParseSites("const x = new Date();"), []);
  assert.deepEqual(rawParseSites("const x = new Date(epoch * 1000);"), []);

  const offenders = [];
  for (const name of AUDITED_MODULES) {
    const src = readFileSync(join(HERE, name), "utf8");
    const sites = rawParseSites(src);
    if (sites.length > 0) offenders.push(`${name}: ${sites.join(", ")}`);
  }
  assert.deepEqual(offenders, []);
});
