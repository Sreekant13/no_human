import test from "node:test";
import assert from "node:assert/strict";
import {
  TRACKERS, configuredTrackers, mergeTrackerResults, sourcesLine, noTrackerMessage,
} from "./backlogSources.js";

// The Backlog page reads two trackers. These are the rules for which it asks,
// how the answers combine, and — the one that had already gone wrong — what it
// is allowed to SAY about a tracker it is not showing.

const REGISTRY = (...on) => ({
  integrations: TRACKERS.map((t) => ({ name: t.name, configured: on.includes(t.name) })),
});

test("only trackers the server calls configured are asked", () => {
  assert.deepEqual(configuredTrackers(REGISTRY("jira")), ["jira"]);
  assert.deepEqual(configuredTrackers(REGISTRY("linear")), ["linear"]);
  assert.deepEqual(configuredTrackers(REGISTRY("jira", "linear")), ["jira", "linear"]);
  assert.deepEqual(configuredTrackers(REGISTRY()), []);
});

test("an unknown or malformed registry yields no trackers rather than guessing", () => {
  assert.deepEqual(configuredTrackers(undefined), []);
  assert.deepEqual(configuredTrackers({}), []);
  assert.deepEqual(configuredTrackers({ integrations: [null, { name: "github", configured: true }] }), []);
  assert.deepEqual(configuredTrackers({ integrations: [{ name: "linear" }] }), [],
    "present-but-unconfigured is not configured");
});

test("configuredTrackers keeps display order, not registry order", () => {
  const reversed = { integrations: [
    { name: "linear", configured: true }, { name: "jira", configured: true },
  ] };
  assert.deepEqual(configuredTrackers(reversed), ["jira", "linear"]);
});

// ── merging ───────────────────────────────────────────────────────────────

const J = (key, updated) => ({ tracker: "jira", key, summary: key, updated });
const L = (key, updated) => ({ tracker: "linear", key, summary: key, updated });

test("both trackers' tickets land in ONE list, newest first", () => {
  const { issues, errors } = mergeTrackerResults([
    { tracker: "jira", issues: [J("P-1", "2026-08-01T10:00:00Z"), J("P-2", "2026-07-01T10:00:00Z")] },
    { tracker: "linear", issues: [L("NO-1", "2026-08-03T10:00:00Z")] },
  ]);
  assert.deepEqual(issues.map((i) => i.key), ["NO-1", "P-1", "P-2"]);
  assert.deepEqual(errors, []);
});

test("a row with no timestamp sorts last — it cannot claim recency", () => {
  const { issues } = mergeTrackerResults([
    { tracker: "jira", issues: [J("P-1", null), J("P-2", "2026-07-01T10:00:00Z")] },
  ]);
  assert.deepEqual(issues.map((i) => i.key), ["P-2", "P-1"]);
});

test("a tracker that FAILED contributes an error and no rows — never an empty list", () => {
  // The lie this forbids: folding a failure into "no tickets". The backlog is
  // not empty, it could not be read, and only one of those is safe to show
  // someone deciding what to work on.
  const { issues, errors } = mergeTrackerResults([
    { tracker: "jira", issues: [J("P-1", "2026-08-01T10:00:00Z")] },
    { tracker: "linear", error: "Linear API key rejected." },
  ]);
  assert.deepEqual(issues.map((i) => i.key), ["P-1"], "the healthy tracker's tickets still show");
  assert.equal(errors.length, 1);
  assert.deepEqual(errors[0], {
    tracker: "linear", label: "Linear", message: "Linear API key rejected.",
  });
});

test("every tracker failing yields no rows and every reason", () => {
  const { issues, errors } = mergeTrackerResults([
    { tracker: "jira", error: "Jira search failed." },
    { tracker: "linear", error: "Linear is rate-limiting." },
  ]);
  assert.deepEqual(issues, []);
  assert.deepEqual(errors.map((e) => e.label), ["Jira", "Linear"]);
});

test("keyless junk is dropped rather than rendered as a row with no ticket", () => {
  const { issues } = mergeTrackerResults([
    { tracker: "jira", issues: [null, { summary: "no key" }, J("P-1", "2026-08-01T10:00:00Z")] },
  ]);
  assert.deepEqual(issues.map((i) => i.key), ["P-1"]);
  assert.deepEqual(mergeTrackerResults(undefined), { issues: [], errors: [] });
});

// ── the claim the page is allowed to make ─────────────────────────────────

test("the sources line names the trackers shown and, when one is off, says only that", () => {
  assert.equal(sourcesLine(["jira"]),
    "Open tickets from Jira — Linear is not connected.");
  assert.equal(sourcesLine(["linear"]),
    "Open tickets from Linear — Jira is not connected.");
  assert.equal(sourcesLine(["jira", "linear"]), "Open tickets from Jira and Linear.");
  assert.equal(sourcesLine([]), null, "the not-connected page owns the nothing-configured case");
  assert.equal(sourcesLine(undefined), null);
});

test("the sources line never explains a tracker's absence with a claim about the code", () => {
  // The regression this pins. The page shipped "the Linear side has no issue
  // listing yet" — a statement about LinearAdapter that was false when it was
  // written: search() was a working paginating GraphQL listing, and only the
  // HTTP route was missing. Whether the ROUTE exists is not the operator's
  // question and not something a config-derived line can know, so the only
  // claim made about an absent tracker is that it is not connected.
  for (const configured of [[], ["jira"], ["linear"], ["jira", "linear"]]) {
    const line = sourcesLine(configured) || "";
    for (const forbidden of [/listing/i, /endpoint/i, /support/i, /cannot/i, /yet\b/i, /no_human/i]) {
      assert.doesNotMatch(line, forbidden,
        `the sources line must not claim ${forbidden} about a tracker: "${line}"`);
    }
  }
});

test("the nothing-configured message blames neither tracker in particular", () => {
  const m = noTrackerMessage();
  assert.match(m, /No tracker is connected/);
  assert.doesNotMatch(m, /Jira|Linear/,
    "with both off, singling one out reads as 'the other one works'");
});
