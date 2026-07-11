// The north-star row for the Stats page (D1): the numbers a staff engineer
// actually trusts, derived from /api/metrics — NOT "completed tasks", which
// counts 0-token code reviews as deliveries. Pure so node --test can pin it.

import { estimateCost, fmtTokens } from "./cost.js";

// tone: "good" | "bad" | "neutral" drives the semantic color of the value.
export function northStarTiles(metrics) {
  if (!metrics) return [];
  const merged = metrics.prs_merged ?? 0;
  const opened = metrics.prs_opened ?? 0;
  const tpp = metrics.tokens_per_pr;
  const rPass = metrics.review_pass ?? 0;
  const rFail = metrics.review_fail ?? 0;
  const share = metrics.cache_economics?.creation_share;
  return [
    {
      label: "PRs merged",
      value: String(merged),
      sub: `${opened} opened · human-merged`,
      tone: merged > 0 ? "good" : "bad",
    },
    {
      label: "Tokens / merged PR",
      value: tpp != null && merged > 0 ? fmtTokens(tpp) : "—",
      sub: tpp != null && merged > 0 ? `est. ${estimateCost(tpp)}` : "no merge yet",
      tone: "neutral",
    },
    {
      label: "Review gate",
      value: rPass + rFail > 0 ? `${rPass}/${rPass + rFail}` : "—",
      sub: `${rFail} blocked by the reviewer`,
      tone: rPass + rFail === 0 ? "neutral" : rFail > rPass ? "bad" : "good",
    },
    {
      label: "Cache reuse",
      value: share != null ? `${((1 - share) * 100).toFixed(0)}%` : "—",
      sub: share != null ? `${(share * 100).toFixed(1)}% rebuilt (full price)` : "no data",
      tone: share == null ? "neutral" : share > 0.5 ? "bad" : "good",
    },
  ];
}
