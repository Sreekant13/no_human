// Token formatting + indicative cost (W2.5). One home so the board, the
// drawer and Stats all say the same number — spend must be visible where
// approval decisions happen, not only on an aggregate page.

export function fmtTokens(n) {
  if (n == null) return "—";
  if (n < 1000) return `${n}`;
  if (n < 1000000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1000000).toFixed(2)}M`;
}

// Rough cost estimate for subscription tokens — purely indicative
// (~$0.003 per 1k tokens as a blended-usage proxy).
export function estimateCost(tokens) {
  if (tokens == null || tokens === 0) return "—";
  const est = (tokens / 1000) * 0.003;
  if (est < 0.01) return "<$0.01";
  return `$${est.toFixed(2)}`;
}
