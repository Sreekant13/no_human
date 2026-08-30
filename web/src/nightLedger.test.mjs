import test from "node:test";
import assert from "node:assert/strict";
import { ledgerSummary, LEDGER_WINDOW_MS } from "./nightLedger.js";

const NOW = new Date("2026-07-17T06:00:00Z").getTime();
const hoursAgo = (h) => new Date(NOW - h * 3600_000).toISOString();

function t(status, hours, extra = {}) {
  return { status, updated_at: hoursAgo(hours), ...extra };
}

test("counts done / real-failed / needs-you inside the window only", () => {
  const s = ledgerSummary([
    t("done", 2),
    t("done", 30),                       // outside the 24h window
    t("failed", 3),
    t("failed", 4, { cancelled: true }), // cancels are not failures (M2)
    t("awaiting_input", 1),
    t("implementing", 1),                // in flight — not a ledger event
  ], NOW);
  assert.equal(s.done, 1);
  assert.equal(s.failed, 1);
  assert.equal(s.parked, 1);
  assert.equal(s.quiet, false);
});

test("cost sums the API's cost_usd across every task in the window", () => {
  // ledgerSummary sums `taskCost(t)` per task, and taskCost is now a pure read of
  // `t.cost_usd` — the API (core/cost.py) already priced coder + reviewer + aux
  // per-model before this field ever reaches the board. PR #104 review found the
  // ledger once reading attempt-level names that do not exist on tasks — priced
  // everything at $0 with a test written to the bug; the guard now is that the
  // ledger's total is exactly the sum of each task's own API-priced cost_usd.
  const s = ledgerSummary([
    t("done", 1, { cost_usd: 3.30, cost_model: "claude-sonnet-5" }),
    t("done", 2, { cost_usd: 1.75, cost_model: "gpt-5.3-codex" }),
  ], NOW);
  assert.equal(s.cost, 5.05);
  assert.ok(s.cost > 0);
});

test("a window with no events AND no spend is quiet; spend alone is not", () => {
  const quiet = ledgerSummary([t("done", 48), t("implementing", 1)], NOW);
  assert.equal(quiet.quiet, true);
  assert.equal(quiet.cost, 0);
  // An in-flight task that burned real tokens is NOT a quiet night (review
  // finding 4: "quiet" must never hide spend).
  const burning = ledgerSummary([t("implementing", 1, { cost_usd: 1.5 })], NOW);
  assert.equal(burning.quiet, false);
  assert.ok(burning.cost > 0);
});

test("missing updated_at falls back to created_at; missing both = excluded", () => {
  const withCreated = { status: "done", created_at: hoursAgo(2) };
  const withNeither = { status: "done" };
  const s = ledgerSummary([withCreated, withNeither], NOW);
  assert.equal(s.done, 1);
});

test("slight future clock skew still counts (no upper bound)", () => {
  const s = ledgerSummary([{ status: "done",
    updated_at: new Date(NOW + 5000).toISOString() }], NOW);
  assert.equal(s.done, 1);
});

test("window is configurable", () => {
  const s = ledgerSummary([t("done", 30)], NOW, 48 * 3600_000);
  assert.equal(s.done, 1);
  assert.equal(LEDGER_WINDOW_MS, 24 * 3600_000);
});
