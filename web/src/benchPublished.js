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
