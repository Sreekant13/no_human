import test from "node:test";
import assert from "node:assert/strict";
import { northStarTiles } from "./northStar.js";

test("empty metrics yields no tiles", () => {
  assert.deepEqual(northStarTiles(null), []);
});

test("zero merged PRs reads as bad; merged reads good", () => {
  const t0 = northStarTiles({ prs_merged: 0, prs_opened: 3, review_pass: 4, review_fail: 15 });
  const merged = t0.find((t) => t.label === "PRs merged");
  assert.equal(merged.value, "0");
  assert.equal(merged.tone, "bad");
  const review = t0.find((t) => t.label === "Review gate");
  assert.equal(review.value, "4/19");
  assert.equal(review.tone, "bad");   // more blocked than passed
  const t1 = northStarTiles({ prs_merged: 2, prs_opened: 2 });
  assert.equal(t1.find((t) => t.label === "PRs merged").tone, "good");
});

test("tokens/PR only shows once something merged; cache share inverts", () => {
  const t = northStarTiles({ prs_merged: 1, tokens_per_pr: 2_000_000,
    cache_economics: { creation_share: 0.0335 } });
  assert.ok(t.find((x) => x.label === "Tokens / merged PR").value.includes("M"));
  const cache = t.find((x) => x.label === "Cache reuse");
  assert.equal(cache.value, "97%");   // 1 - 0.0335
  assert.equal(cache.tone, "good");
  // no merge → tokens/PR is a dash, not a misleading number
  const t2 = northStarTiles({ prs_merged: 0, tokens_per_pr: 5000 });
  assert.equal(t2.find((x) => x.label === "Tokens / merged PR").value, "—");
});
