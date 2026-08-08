// SCRUM-82: the bench panel leads with a REFUSED probe (honest, keep it) but
// never answers "what is the last trusted number". This selector surfaces
// the last PUBLISHED baseline — sourced ONLY from a run the backend marked
// `published === true` — so the headline row above the refusal card can show
// a trusted figure without ever fabricating one. Pure so node --test can pin it.

// Returns the trusted published-baseline figures, or null when there is none.
// `published !== true` (missing, false, or absent entirely) means the payload
// is not a clean published baseline — either a refused/quick/probe run, or an
// install where a clean baseline exists but predates this field being wired
// up. Either way: never source figures from it.
export function selectPublishedRun(bench) {
  if (!bench || bench.norun || bench.published !== true) return null;
  return {
    label: bench.label,
    created_at: bench.created_at,
    success_rate: bench.success_rate,
    satisfied: bench.satisfied,
    total: bench.total,
    skipped: bench.skipped,
    // bench-v2 V1. Carried through explicitly because this projection is a
    // WHITELIST: a field the backend publishes but this list omits is silently
    // absent downstream, and `benchSuccessText` would then fall back to the
    // pooled rate with no interval — publishing the exact bare percentage the
    // interval exists to replace, on the one row labelled "trusted".
    spec_mean_success_rate: bench.spec_mean_success_rate,
    success_ci_low: bench.success_ci_low,
    success_ci_high: bench.success_ci_high,
    trials: bench.trials,
    specs: bench.specs,
    ran_specs: bench.ran_specs,
    // P0.1: the headline rate divides over the specs that DID PRICED WORK
    // (deaths excluded), and this pair is that population's own fraction —
    // dropping it here would silently send the fraction below back to the
    // pooled pair the headline no longer matches.
    measured_specs: bench.measured_specs,
    measured_satisfied: bench.measured_satisfied,
    dead_specs: bench.dead_specs,
    dead_spec_count: bench.dead_spec_count,
    honest_escalations: bench.honest_escalations,
    escalation_specs: bench.escalation_specs,
    honest_escalation_rate: bench.honest_escalation_rate,
    median_cost_ratio: bench.median_cost_ratio,
  };
}

// Success is measured over RAN specs (total - skipped), never the loaded
// total: eval/northstar_card.py divides satisfied by len(ran), and `ran`
// excludes skipped specs. Rendering `satisfied/total` invents a denominator
// the backend never published (v13: 25/56 = 44.6%, not the published 47%).
// `skipped` must be a finite number to trust the subtraction — otherwise omit
// the fraction entirely rather than print a wrong one.
export function formatSuccessFraction(satisfied, total, skipped) {
  if (!Number.isFinite(satisfied) || !Number.isFinite(total) || !Number.isFinite(skipped)) {
    return "";
  }
  return ` (${satisfied}/${total - skipped})`;
}

// Mirrors BenchTrust's honest-escalation logic: prefer the raw
// escalations/specs ratio (with its denominator shown) when the denominator
// is known and non-zero, else fall back to the pre-computed rate — flagged
// "denominator unknown" either way, same as BenchTrust, so a bare "100%"/"—"
// is never mistaken for a measured ratio.
export function publishedEscalationPct(row) {
  if (!row) return "—";
  const n = row.honest_escalations;
  const d = row.escalation_specs;
  if (Number.isFinite(n) && Number.isFinite(d) && d > 0) {
    return `${Math.round((n / d) * 100)}% (${n}/${d})`;
  }
  const fallback = Number.isFinite(row.honest_escalation_rate)
    ? `${Math.round(row.honest_escalation_rate * 100)}%`
    : "—";
  return `${fallback} (denominator unknown)`;
}

// --------------------------------------------------------------------------
// bench-v2 V1 review (SHOULD 4). Two defects, one root cause: this panel read
// the card's aggregate as if every field were counted in SPECS. Two are not.
// --------------------------------------------------------------------------

const fin = (v) => typeof v === "number" && Number.isFinite(v);

// `total`, `skipped` and `dead_specs` count recorded ROWS — one per
// (spec, trial). Under `--trials 3` that is 3x the spec count, so a run that
// loaded 20 specs of a 55-spec corpus rendered "Corpus coverage: 60 of 55
// loaded": a filtered slice reading as MORE than the whole corpus, in the one
// widget that exists to make under-coverage visible. `specs`, `ran_specs` and
// `dead_spec_count` are the spec-wise counts the backend publishes for exactly
// this comparison. The row fields stay as the fallback for a card written
// before they existed — where rows and specs are the same number anyway, so
// the fallback is not an approximation, it is the identity.
export function benchSpecCounts(card) {
  const rows = fin(card?.total) ? card.total : 0;
  const skippedRows = fin(card?.skipped) ? card.skipped : 0;
  const specs = fin(card?.specs) ? card.specs : rows;
  const ranSpecs = fin(card?.ran_specs)
    ? card.ran_specs
    : Math.max(0, rows - skippedRows);
  // A spec that died in one trial and ran in another is ONE dead spec, not
  // three; `dead_specs` counts the rows. Same fallback rule.
  const deadSpecs = fin(card?.dead_spec_count)
    ? card.dead_spec_count
    : (fin(card?.dead_specs) ? card.dead_specs : 0);
  const neverRan = Math.max(0, specs - ranSpecs);
  const trials = fin(card?.trials) && card.trials >= 1 ? card.trials : 1;
  return {
    specs, ranSpecs, deadSpecs, trials,
    skippedSpecs: neverRan,
    // Specs that yielded no measurement: never ran, or ran and burned nothing.
    unmeasured: neverRan + deadSpecs,
  };
}

// The headline percentage AND its interval, from ONE estimator.
//
// The backend's `success_headline` publishes the mean of per-spec means with a
// Wilson interval carried onto the clustering-adjusted effective n. A panel
// that prints `success_rate` (the pooled ROW rate) next to that interval
// re-creates the exact defect the interval was added to fix — on a resumed
// 12-spec run the two differ by eight points and the interval excludes the
// percentage in front of it. Prefer `spec_mean_success_rate`; fall back to
// `success_rate` only for a card written before it existed, where the two are
// equal by construction.
//
// A card with no interval renders as a bare percentage and that is correct:
// it never measured one, and inventing a range for it would be worse than
// omitting it. `nh bench publish` refuses such a file separately.
export function benchSuccessText(card) {
  const rate = fin(card?.spec_mean_success_rate)
    ? card.spec_mean_success_rate
    : (fin(card?.success_rate) ? card.success_rate : null);
  const base = rate === null ? "—" : `${Math.round(rate * 100)}%`;
  const lo = card?.success_ci_low;
  const hi = card?.success_ci_high;
  if (!fin(lo) || !fin(hi)) return base;
  return `${base} (95% CI ${(lo * 100).toFixed(1)}–${(hi * 100).toFixed(1)})`;
}

// What the percentage was measured over. At one trial per spec this is the
// familiar `(satisfied/ran)` pair, now with the SPEC count as its denominator
// rather than `total - skipped` in rows — identical at one trial, right at N.
// Above one trial there is no honest integer numerator to print: `satisfied`
// counts passing TRIALS (220 of 221) while the percentage in front is a mean
// of per-spec means (91.7%), and rendering "220/12" or "220/221" beside it
// invites the reader to check an arithmetic that does not hold. So the shape
// of the sample is stated instead of a fraction that would not reduce to it.
export function benchSuccessDenominator(card) {
  const { specs, ranSpecs, skippedSpecs, trials } = benchSpecCounts(card);
  if (!ranSpecs) return "";
  if (trials > 1) return ` (over ${ranSpecs} specs × ${trials} trials)`;
  // P0.1: the percentage in front of this fraction is computed over the specs
  // that DID PRICED WORK — deaths excluded — so the fraction must be the same
  // population or it stops reducing to it (a 1-dead card would print 25/53
  // beside a rate measured over 52). The backend publishes that population's
  // own pair (`measured_satisfied`/`measured_specs`); at one trial per spec it
  // reduces to the headline exactly. Cards written before those keys existed
  // fall back to the ran pair — on such cards the headline itself predates the
  // measured population, so the pair still matches what stands in front of it.
  if (fin(card?.measured_satisfied) && fin(card?.measured_specs)) {
    return formatSuccessFraction(card.measured_satisfied, card.measured_specs, 0);
  }
  // Through `formatSuccessFraction`, not a second copy of `${a}/${b}`: it owns
  // the rule that the denominator is RAN (total minus skipped) and the rule
  // that a non-finite input omits the fraction rather than printing a wrong
  // one. What changes here is only the unit fed to it — specs, not rows.
  return formatSuccessFraction(card?.satisfied, specs, skippedSpecs);
}
