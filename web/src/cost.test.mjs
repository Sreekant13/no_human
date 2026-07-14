import test from "node:test";
import assert from "node:assert/strict";
import { fmtTokens, totalBurn, costOf, fmtCost, lifetimeCost } from "./cost.js";

test("totalBurn is every token bucket — cache-read (the hidden 90%) AND cache-creation", () => {
  // Named buckets, like costOf: the burn a card shows and the cost it is priced at must have
  // the SAME basis. They didn't — the Token Usage tile read "169.87M · est. $73.58", pricing
  // 6M cache-creation tokens the count never showed.
  assert.equal(totalBurn({ used: 121_500, read: 33_000_000 }), 33_121_500);
  assert.equal(totalBurn({ used: 121_500, creation: 6_000, read: 33_000_000 }), 33_127_500);
  assert.equal(totalBurn({}), 0);
  assert.equal(totalBurn(null), 0);
  assert.equal(totalBurn({ used: 500 }), 500);
});

test("cost blends fresh and cache-read pricing", () => {
  // 1M fresh = $3.00; 1M cache-read = $0.30
  assert.equal(fmtCost(costOf({ used: 1_000_000 })), "$3.00");
  assert.equal(fmtCost(costOf({ read: 1_000_000 })), "$0.30");
  assert.equal(fmtCost(costOf({ used: 0, read: 0 })), "—");
  // REMOVED: the old `estimateCost(tokens, cacheRead = 0)` had a defaulted second argument,
  // and this suite USED to assert that "single-arg callers keep working" — i.e. it pinned
  // the footgun open. A one-argument call priced a TOTAL burn at the fresh rate, which is
  // exactly the bug. The buckets are named now; they cannot be dropped or transposed.
});

test("fmtTokens", () => {
  assert.equal(fmtTokens(33_121_500), "33.12M");
  assert.equal(fmtTokens(null), "—");
});

// ONE cost model, three buckets — the only way the per-PR tile and the lifetime tile can
// agree. Pricing history: the tile priced a TOTAL burn at the FRESH rate ($29.98/PR), then
// split that total by `creation_share` ($3.93/PR) — but `tokens_per_pr` contains no
// cache-creation at all, and `creation_share`'s universe is creation+read, so the two tiles
// still implied blended rates 20% apart. Fresh = in/out + cache-creation (both full price);
// cache-read is a tenth.
test("costOf prices the three buckets: in/out and cache-creation full, cache-read at 1/10", () => {
  // 1M in/out + 1M creation @ $0.003/1k = $3.00 + $3.00; 10M read @ $0.0003/1k = $3.00
  assert.equal(costOf({ used: 1_000_000, creation: 1_000_000, read: 10_000_000 }), 9);
  assert.equal(costOf({ used: 0, creation: 0, read: 0 }), 0);
  assert.equal(costOf({}), 0);
  assert.equal(costOf(null), 0);
});

test("costOf on the LIVE lifetime buckets — the honest number", () => {
  // /api/metrics: tokens_used_total 1,694,616 · creation 6,014,457 · read 168,178,356
  const lifetime = costOf({ used: 1_694_616, creation: 6_014_457, read: 168_178_356 });
  assert.equal(fmtCost(lifetime), "$73.58");
  // ...and per merged PR (13) it is $5.66 — NOT the $3.93 the split model produced, and not
  // the $29.98 the fresh-rate model produced.
  assert.equal(fmtCost(lifetime / 13), "$5.66");
});

// (A test asserting "both tiles imply the same $/M" was written here and DELETED: both sides
// were derived from the same `costOf` scalar, so it asserted x === x and passed even when
// costOf dropped the cache-creation bucket entirely. The guard that actually works is the test
// above, which pins the dollar figure independently: mutate the model and it fails.)

test("fmtCost formats, and says nothing when there is nothing to say", () => {
  assert.equal(fmtCost(0), "—");
  assert.equal(fmtCost(null), "—");
  assert.equal(fmtCost(undefined), "—");
  assert.equal(fmtCost(0.004), "<$0.01");
  assert.equal(fmtCost(12.345), "$12.35");
});

// The reviewer's burn is now on the record (it used to be discarded after the verdict), so the
// tiles can price the WHOLE run instead of the coder half — 59 Opus-4-8 review passes over full
// diffs used to cost $0 on the page.
test("lifetimeCost prices the coder AND the reviewer", () => {
  const metrics = {
    tokens_used_total: 1_000_000,
    cache_economics: { cache_creation_total: 0, cache_read_total: 0 },
    review_tokens_used_total: 1_000_000,
    review_cache_creation_total: 0,
    review_cache_read_total: 10_000_000,
  };
  // coder 1M fresh = $3.00 · reviewer 1M fresh = $3.00 + 10M read = $3.00  →  $9.00
  assert.equal(fmtCost(lifetimeCost(metrics)), "$9.00");
});
