// SCRUM-25: the Benchmark panel used to headline whatever /api/bench/latest
// returned — including a 1-spec health probe force-published over the real
// baseline, which read "SUCCESS 0% (0/1)" as the first thing a reader saw.
// The instrument's job is to show the measurement the gate actually accepted;
// a newer refused/probe run is real information, but it is a footnote, not
// the headline. Pure so node --test can pin it.

// `published`: every published-baseline card known to the caller (today's
// API sends at most one, but the selector does not assume that — a reader
// picks the most recent by date, not "the one it was handed"). `latestRun`:
// the most recent run recorded, whether or not it ever became the headline.
export function selectBenchHeadline(published, latestRun) {
  const candidates = (Array.isArray(published) ? published : []).filter(Boolean);
  if (candidates.length === 0) return { headline: null, footnote: null };

  const headline = candidates.reduce((best, cur) =>
    !best || String(cur.created_at) > String(best.created_at) ? cur : best);

  // A footnote only when latestRun is a DIFFERENT, STRICTLY NEWER run than the
  // headline — otherwise "the latest run" IS the published baseline (or an
  // older one) and there is nothing else to report below it.
  const footnote =
    latestRun && String(latestRun.created_at) > String(headline.created_at)
      ? latestRun
      : null;

  return { headline, footnote };
}

// The footnote's reasons: why the newer run did NOT become the baseline.
// Prefers `refusals` (the intrinsic reasons a run may not publish); falls
// back to `override_reasons` (what a forced publish overrode) so a run that
// WAS force-published over refusals — and therefore carries no `refusals` of
// its own — still explains itself.
export function footnoteReasons(footnote) {
  if (!footnote) return [];
  if (Array.isArray(footnote.refusals) && footnote.refusals.length) return footnote.refusals;
  if (Array.isArray(footnote.override_reasons) && footnote.override_reasons.length)
    return footnote.override_reasons;
  return [];
}

export function footnoteLabel(footnote) {
  const reasons = footnoteReasons(footnote);
  return `latest run - refused publication: ${reasons.length ? reasons.join("; ") : "not publishable"}`;
}
