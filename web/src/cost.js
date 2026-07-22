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
 * One task's token burn across the same nine buckets {@link taskCost} prices — so a surface
 * showing both cannot show a price for tokens its own count never included. That mismatch is
 * on the record in this file: "169.87M · est. $73.58", a price for 6M tokens the count never
 * showed. It recurred twice here: the Stats Token Usage tile and the task table's Tokens
 * column each counted the coder's buckets beside a whole-run price.
 */
export function taskBurn(task) {
  if (!task) return 0;
  return (
    totalBurn({ used: task.total_tokens, creation: task.total_cache_creation, read: task.total_cache_read })
    + totalBurn({
      used: task.total_review_tokens,
      creation: task.total_review_cache_creation,
      read: task.total_review_cache_read,
    })
    + totalBurn({
      used: task.total_aux_tokens,
      creation: task.total_aux_cache_creation,
      read: task.total_aux_cache_read,
    })
  );
}

/**
 * One task's cost: coder + reviewer + aux (planning/utility) — the per-task twin of
 * {@link lifetimeCost}, which prices the same nine buckets from the metrics payload. All
 * nine that TaskSummaryOut sends, because the API sends them so "the task row prices the
 * WHOLE run, not just coder+review" (models.py). If you add a bucket to one of these, add it
 * to the other, or a page showing both will contradict itself.
 */
export function taskCost(task) {
  if (!task) return 0;
  return (
    costOf({ used: task.total_tokens, creation: task.total_cache_creation, read: task.total_cache_read })
    + costOf({
      used: task.total_review_tokens,
      creation: task.total_review_cache_creation,
      read: task.total_review_cache_read,
    })
    + costOf({
      used: task.total_aux_tokens,
      creation: task.total_aux_cache_creation,
      read: task.total_aux_cache_read,
    })
  );
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
  // The coder AND the gate AND aux. The reviewer's tokens used to be discarded after the
  // verdict, so every cost surface priced the coder half of the run and called it "spend" —
  // 59 Opus-4-8 review passes over full diffs cost $0 on the page. Aux was the same story one
  // bucket later: metrics.py ships aux_*_total, this function ignored them, and the moment
  // taskCost started pricing aux the per-task rollup on Stats exceeded the lifetime tile
  // directly above it. Same nine buckets as {@link taskCost}, or the page contradicts itself.
  return (
    costOf({ used, creation: ce.cache_creation_total, read: ce.cache_read_total })
    + costOf({
      used: metrics.review_tokens_used_total,
      creation: metrics.review_cache_creation_total,
      read: metrics.review_cache_read_total,
    })
    + costOf({
      used: metrics.aux_tokens_used_total,
      creation: metrics.aux_cache_creation_total,
      read: metrics.aux_cache_read_total,
    })
  );
}
