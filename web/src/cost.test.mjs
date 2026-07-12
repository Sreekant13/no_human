import test from "node:test";
import assert from "node:assert/strict";
import { estimateCost, fmtTokens, totalBurn } from "./cost.js";

test("totalBurn includes cache reads — the 90% that was hidden", () => {
  assert.equal(totalBurn(121_500, 33_000_000), 33_121_500);
  assert.equal(totalBurn(null, null), 0);
  assert.equal(totalBurn(500, undefined), 500);
});

test("estimateCost blends fresh and cache-read pricing", () => {
  // 1M fresh = $3.00; 1M cache-read = $0.30
  assert.equal(estimateCost(1_000_000, 0), "$3.00");
  assert.equal(estimateCost(0, 1_000_000), "$0.30");
  assert.equal(estimateCost(0, 0), "—");
  // single-arg callers keep working
  assert.equal(estimateCost(1_000_000), "$3.00");
});

test("fmtTokens", () => {
  assert.equal(fmtTokens(33_121_500), "33.12M");
  assert.equal(fmtTokens(null), "—");
});
