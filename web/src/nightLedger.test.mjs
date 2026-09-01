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

// The old "cost sums cost_usd across tasks in the window" test's premise WAS
// the bug: it filtered tasks by `updated_at` and summed each survivor's
// LIFETIME `cost_usd`, so closing an old task with no new spend swept its
// whole historical cost into "last 24h". `ledgerSummary` no longer reads
// `cost_usd`/`total_tokens` off tasks at all — `spend` (the server's
// attempt-attributed `/api/metrics/window` payload) is the only source.
test("cost and tokens are the server's window figure, never re-derived from task rows", () => {
  const s = ledgerSummary([
    t("done", 1, { cost_usd: 999, total_tokens: 999_999 }),
    t("done", 2, { cost_usd: 999, total_tokens: 999_999 }),
  ], NOW, undefined, { cost_usd: 1.25, tokens: 40 });
  assert.equal(s.cost, 1.25);
  assert.equal(s.tokens, 40);
});

test("a task closed in the window with lifetime cost contributes nothing to the ledger's cost", () => {
  // JS mirror of repro #1: a task with a large lifetime cost_usd, closed
  // (touched) inside the window, but the server says nothing NEW was spent.
  const s = ledgerSummary(
    [t("failed", 1, { cost_usd: 18.68 })], NOW, undefined,
    { cost_usd: 0, tokens: 0 },
  );
  assert.equal(s.cost, 0);
  assert.equal(s.tokens, 0);
  assert.equal(s.failed, 1); // the event itself still counts — only cost is zeroed
});

test("no server figure (old daemon, 404) shows no spend line rather than a wrong one", () => {
  const s = ledgerSummary([t("done", 1)], NOW, undefined, null);
  assert.equal(s.cost, 0);
  assert.equal(s.tokens, 0);
  assert.equal(s.done, 1);
});

test("a window with no events AND no spend is quiet; spend alone is not", () => {
  const quiet = ledgerSummary([t("done", 48), t("implementing", 1)], NOW);
  assert.equal(quiet.quiet, true);
  assert.equal(quiet.cost, 0);
  // An in-flight task that burned real tokens is NOT a quiet night (review
  // finding 4: "quiet" must never hide spend) — the burn is now reported by
  // the server's window figure, not a per-task field.
  const burning = ledgerSummary([t("implementing", 1)], NOW, undefined, { cost_usd: 1.5, tokens: 500 });
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
