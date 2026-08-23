import test from "node:test";
import assert from "node:assert/strict";
import { northStarTiles } from "./northStar.js";

test("empty metrics yields no tiles", () => {
  assert.deepEqual(northStarTiles(null), []);
});

test("zero merged PRs reads as bad; a non-zero count is NOT 'good'", () => {
  const t0 = northStarTiles({ prs_merged: 0, prs_opened: 3, review_pass: 4, review_fail: 15 });
  const merged = t0.find((t) => t.label === "PRs merged");
  assert.equal(merged.value, "0");
  assert.equal(merged.tone, "bad");   // nothing has shipped — a real alarm
  const review = t0.find((t) => t.label === "Review gate");
  assert.equal(review.value, "4/19");
  assert.equal(review.tone, "bad");   // more blocked than passed
  // CHANGED (UI_AUDIT M9): "2 merged" used to read GREEN, i.e. green meant "non-zero".
  // With four tiles green at once the colour stopped carrying any signal, so a raw count
  // with no threshold for "good" is now neutral.
  const t1 = northStarTiles({ prs_merged: 2, prs_opened: 2 });
  assert.equal(t1.find((t) => t.label === "PRs merged").tone, "neutral");
});

test("cost/PR only shows once something merged; cache share inverts", () => {
  const t = northStarTiles({
    prs_merged: 2, tokens_used_total: 1_000_000,
    cache_economics: { creation_share: 0.0335, cache_creation_total: 0, cache_read_total: 10_000_000 },
  });
  // $3.00 (1M in/out) + $3.00 (10M read @ 1/10) = $6.00 over 2 merged = $3.00
  const cost = t.find((x) => x.label === "Cost / merged PR");
  assert.equal(cost.value, "$3.00");
  assert.match(cost.sub, /\$6\.00 spent · 2 merged/);
  const cache = t.find((x) => x.label === "Cache reuse");
  assert.equal(cache.value, "97%");   // 1 - 0.0335
  assert.equal(cache.tone, "good");
  // no merge → a dash, not a misleading number
  const t2 = northStarTiles({ prs_merged: 0, tokens_used_total: 5000 });
  assert.equal(t2.find((x) => x.label === "Cost / merged PR").value, "—");
});

// M1 — the cost lie. `estimateCost(tokens_per_pr)` priced a TOTAL burn at the fresh rate.
test("the cost per merged PR is the LIFETIME cost over merged PRs — not tokens_per_pr", () => {
  // The live buckets. tokens_per_pr (9.99M) is a per-OPENED-PR figure that excludes
  // cache-creation entirely — pricing it, however cleverly, cannot produce this number.
  const tiles = northStarTiles({
    prs_merged: 13, prs_opened: 17, tokens_per_pr: 9_992_527,
    tokens_used_total: 1_694_616,
    cache_economics: {
      creation_share: 0.0345,
      cache_creation_total: 6_014_457,
      cache_read_total: 168_178_356,
    },
  });
  const cost = tiles.find((t) => t.label === "Cost / merged PR");
  assert.equal(cost.value, "$5.66");            // NOT $3.93 (split model), NOT $29.98 (fresh-rate)
  assert.match(cost.sub, /\$73\.58 spent · 13 merged/);
});

test("no token data → the cost tile says so rather than guessing", () => {
  const tiles = northStarTiles({ prs_merged: 13 });
  assert.equal(tiles.find((t) => t.label === "Cost / merged PR").value, "—");
});

test("no measured cache split → the cost tile says so rather than guessing", () => {
  const tiles = northStarTiles({ prs_merged: 13, tokens_per_pr: 9_992_527 });
  const cost = tiles.find((t) => t.label === "Cost / merged PR");
  assert.equal(cost.value, "—");
});

// M9 — the tone system. Colour must mean "measured against a threshold", or it means nothing.
test("volume tiles are neutral; only tiles with a real threshold carry colour", () => {
  const tiles = northStarTiles({
    prs_merged: 13, prs_opened: 17, tokens_per_pr: 1_000_000,
    review_pass: 42, review_fail: 17,
    cache_economics: { creation_share: 0.03 },
  });
  const tone = (label) => tiles.find((t) => t.label === label)?.tone;
  assert.equal(tone("PRs merged"), "neutral", "a raw count has no threshold — green meant 'non-zero'");
  assert.equal(tone("Cost / merged PR"), "neutral", "cost is volume, not quality");
  assert.equal(tone("Review gate"), "good");
  assert.equal(tone("Cache reuse"), "good");
});

// THE ASSERTION WHOSE ABSENCE LET THE PAGE LIE. The "Cost / merged PR" tile and the
// "Token Usage" tile show the same burn from the same buckets; if they ever assemble those
// buckets separately again, their implied $/token drifts apart (it did: $29.98 vs $55.54,
// then $3.93 vs $55.54, then $68.50 vs $73.58). They are ONE identity now.
test("the per-PR tile and the lifetime tile derive from the same cost — they cannot disagree", async () => {
  const { lifetimeCost, fmtCost } = await import("./cost.js");
  const metrics = {
    prs_merged: 13, prs_opened: 17,
    tokens_used_total: 1_694_616,
    cache_economics: { cache_creation_total: 6_014_457, cache_read_total: 168_178_356, creation_share: 0.0345 },
  };
  const tiles = northStarTiles(metrics);
  const perPr = tiles.find((t) => t.label === "Cost / merged PR");
  const lifetime = lifetimeCost(metrics);          // what the Token Usage tile renders
  assert.equal(fmtCost(lifetime), "$73.58");
  assert.equal(perPr.value, fmtCost(lifetime / metrics.prs_merged));
  assert.match(perPr.sub, /\$73\.58 spent · 13 merged/);
});

test("no tokens_used_total (an un-restarted server) → BOTH surfaces say '—', not two numbers", async () => {
  const { lifetimeCost, fmtCost } = await import("./cost.js");
  const metrics = { prs_merged: 13, cache_economics: { cache_creation_total: 1, cache_read_total: 2 } };
  assert.equal(lifetimeCost(metrics), null);
  assert.equal(fmtCost(lifetimeCost(metrics)), "—");
  assert.equal(northStarTiles(metrics).find((t) => t.label === "Cost / merged PR").value, "—");
});

// The per-attempt distribution is a SIBLING tile of the fleet "Cache reuse" tile, not
// a replacement — the earliest signal a single attempt (not the whole fleet) is heading
// for budget exhaustion. Neither label may say "reuse"; that word belongs to the fleet tile.
test("cache-read-share distribution is its own tile, distinct from the fleet 'Cache reuse' tile", () => {
  const tiles = northStarTiles({
    cache_economics: { creation_share: 0.03 },
    cache_read_share_dist: { p50: 0.9, p90: 0.99, attempts_measured: 3 },
  });
  const fleet = tiles.find((t) => t.label === "Cache reuse");
  const perAttempt = tiles.find((t) => t.label !== "Cache reuse" && /cache/i.test(t.label));
  assert.ok(fleet, "fleet tile must still be present");
  assert.ok(perAttempt, "a distinct per-attempt tile must be present");
  assert.notEqual(fleet.label, perAttempt.label);
  assert.doesNotMatch(perAttempt.label.toLowerCase(), /reuse/);
  assert.equal(fleet.value, "97%");            // 1 - 0.03, unaffected by the new tile
  assert.equal(perAttempt.value, "90%");       // p50
  assert.match(perAttempt.sub, /99%/);         // p90 surfaced too
  assert.match(perAttempt.sub, /3/);           // attempts_measured surfaced too
});

test("cache-read-share distribution absent → the tile reads '—', no crash", () => {
  const tiles = northStarTiles({ prs_merged: 1 });
  const perAttempt = tiles.find((t) => t.label !== "Cache reuse" && /cache/i.test(t.label));
  assert.ok(perAttempt);
  assert.equal(perAttempt.value, "—");
  assert.equal(perAttempt.sub, "no data");
});
