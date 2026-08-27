import test from "node:test";
import assert from "node:assert/strict";

import { summaryRepoCounts } from "./onboardingSummary.js";

// BUG (real-user walk, 2026-08-15): after registering exactly one repo, the
// launch summary showed "Repos: 0" beside "Repos with a proven test command:
// 0 of 1" for the SAME repo. The "Repos" row read `selectedRepos.size`
// (this mount's checkboxes) while the proven-test row read the server's
// readiness payload (the persisted profile store) — two different sources
// for the same fact. `summaryRepoCounts` is now the ONLY producer of both
// strings, so they can never diverge again.

test("one registered repo counts as 1 in both rows", () => {
  const counts = summaryRepoCounts({ total: 1, usable: 0 });
  assert.equal(counts.repos, "1");
  assert.equal(counts.proven, "0 of 1");
});

test("no registered repos reads 0 and — (not '0 of 0')", () => {
  // m6: "0 of 0" reads oddly when there are no repos yet — show an em dash.
  const counts = summaryRepoCounts({ total: 0, usable: 0 });
  assert.equal(counts.repos, "0");
  assert.equal(counts.proven, "—");
});

test("both rows are derived from the same readiness object", () => {
  const counts = summaryRepoCounts({ total: 3, usable: 2 });
  assert.equal(counts.repos, "3");
  assert.equal(counts.proven, "2 of 3");
});

test("a fetch failure renders — in both rows, never 'undefined of undefined'", () => {
  const counts = summaryRepoCounts({ error: true });
  assert.equal(counts.repos, "—");
  assert.equal(counts.proven, "—");
});

test("in-flight readiness renders … in both rows", () => {
  const counts = summaryRepoCounts(null);
  assert.equal(counts.repos, "…");
  assert.equal(counts.proven, "…");
});

test("missing/NaN total or usable coerce to 0 rather than propagating undefined", () => {
  const counts = summaryRepoCounts({});
  assert.equal(counts.repos, "0");
  assert.equal(counts.proven, "—");   // m6: no repos ⇒ em dash, not "0 of 0"
});
