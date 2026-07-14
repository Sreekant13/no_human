// Token formatting + indicative cost (W2.5). One home so the board, the drawer and Stats all
// say the same number — spend must be visible where approval decisions happen, not only on an
// aggregate page.

export function fmtTokens(n) {
  if (n == null) return "—";
  if (n < 1000) return `${n}`;
  if (n < 1000000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1000000).toFixed(2)}M`;
}

/**
 * Every token bucket, summed — the number a burn meter shows.
 *
 * Cache-read is 90%+ of real spend (C1): summing only tokens_used under-reported a 33M-token
 * task as "121.5k tok". Cache-CREATION was the bucket still missing, so the burn a surface
 * displayed and the cost it priced had different bases (the Token Usage tile read
 * "169.87M · est. $73.58" — a price for 6M tokens the count never showed).
 *
 * Named buckets, like {@link costOf}: they cannot be transposed or silently dropped.
 */
export function totalBurn(buckets) {
  const { used = 0, creation = 0, read = 0 } = buckets || {};
  return (used || 0) + (creation || 0) + (read || 0);
}

// Indicative rates. Fresh work — in/out tokens AND cache-CREATION — is full price; a cache
// READ is a tenth of it.
const RATE_FRESH_PER_TOKEN = 0.003 / 1000;
const RATE_CACHE_READ_PER_TOKEN = 0.0003 / 1000;

/**
 * The ONE cost model: three buckets, priced in dollars.
 *
 * It takes an OBJECT so the buckets cannot be transposed. The old signature —
 * `estimateCost(tokens, cacheRead = 0)` — let a ONE-argument call silently price a TOTAL burn
 * at the fresh rate, which is how the Stats tile came to claim $29.98 per merged PR. The
 * repair after that (splitting the total by `cache_economics.creation_share`) was also wrong:
 * `tokens_per_pr` contains no cache-creation at all, while `creation_share`'s universe is
 * creation+read — a category error that still left two tiles on one page implying blended
 * rates 20% apart.
 *
 * Every surface (per-PR tile, lifetime tile, task table, drawer header) now divides the SAME
 * number by a different denominator, so they cannot disagree.
 */
export function costOf(buckets) {
  const { used = 0, creation = 0, read = 0 } = buckets || {};
  return (used || 0) * RATE_FRESH_PER_TOKEN
    + (creation || 0) * RATE_FRESH_PER_TOKEN
    + (read || 0) * RATE_CACHE_READ_PER_TOKEN;
}

/** Format a dollar figure — or say nothing, which beats saying a wrong number. */
export function fmtCost(dollars) {
  if (dollars == null || !Number.isFinite(dollars) || dollars === 0) return "—";
  if (dollars < 0.01) return "<$0.01";
  return `$${dollars.toFixed(2)}`;
}

/**
 * The lifetime cost, from /api/metrics — the SINGLE source both the "Cost / merged PR" tile
 * and the "Token Usage" tile read.
 *
 * They must not each assemble their own buckets: when one of them kept a fallback to
 * task-summed tokens and the other didn't, the same page showed $68.50 and $73.58 for the
 * same burn. Returns null when the buckets are not all there — then BOTH tiles say "—"
 * together, which is honest, instead of disagreeing.
 */
export function lifetimeCost(metrics) {
  const used = metrics?.tokens_used_total;
  if (used == null) return null;
  const ce = metrics.cache_economics || {};
  // The coder AND the gate. The reviewer's tokens used to be discarded after the verdict, so
  // every cost surface priced the coder half of the run and called it "spend" — 59 Opus-4-8
  // review passes over full diffs cost $0 on the page.
  return (
    costOf({ used, creation: ce.cache_creation_total, read: ce.cache_read_total })
    + costOf({
      used: metrics.review_tokens_used_total,
      creation: metrics.review_cache_creation_total,
      read: metrics.review_cache_read_total,
    })
  );
}
