// Token formatting + indicative cost (W2.5). One home so the board, the
// drawer and Stats all say the same number — spend must be visible where
// approval decisions happen, not only on an aggregate page.

export function fmtTokens(n) {
  if (n == null) return "—";
  if (n < 1000) return `${n}`;
  if (n < 1000000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1000000).toFixed(2)}M`;
}

// Total burn: cache-read is 90%+ of real spend (C1) — a meter summing only
// tokens_used under-reported a 33M-token task as "121.5k tok".
export function totalBurn(tokens, cacheRead) {
  return (tokens || 0) + (cacheRead || 0);
}

// Rough cost estimate — purely indicative. Fresh tokens at ~$0.003/1k
// (blended in/out proxy), cache reads at ~10% of that.
export function estimateCost(tokens, cacheRead = 0) {
  const burn = totalBurn(tokens, cacheRead);
  if (burn === 0) return "—";
  const est = ((tokens || 0) / 1000) * 0.003 + ((cacheRead || 0) / 1000) * 0.0003;
  if (est < 0.01) return "<$0.01";
  return `$${est.toFixed(2)}`;
}
