// The retire? section (Settings LearningsPanel): client-side labelling and
// dismissal for GET /api/learnings/retire-candidates rows. The server decides
// WHICH rows are stale (`learning/retire.py`'s `retirement_candidates`, days
// = 90 by default); this module only turns each row into a human-readable
// label and remembers which ids the operator already said "not now" to — a
// dismissal is purely client-side (it writes nothing server-side, per the
// suggest-only contract), so it must not resurface until the caller forgets
// it (e.g. a page reload).
//
// Pure functions, `node --test`'d, same shape as learningGroups.js/learningCard.js.

const MS_PER_DAY = 24 * 60 * 60 * 1000;

// A human-readable label for how stale a candidate is. A missing/null/
// unparseable `last_used_at` reads "never used" — genuinely different from
// "unused for N days", the same distinction `retirement_candidates`'
// `never_used` annotation makes server-side, and it must not collapse back
// into one string here.
export function retireLabel(item, { now = Date.now() } = {}) {
  const lastUsed = item && item.last_used_at;
  if (!lastUsed) return "never used";
  const usedMs = Date.parse(lastUsed);
  if (Number.isNaN(usedMs)) return "never used";
  const days = Math.max(0, Math.floor((now - usedMs) / MS_PER_DAY));
  return `unused for ${days} day${days === 1 ? "" : "s"}`;
}

// Candidates ready for the retire? section: stale-only (a fresh row — used
// within `days` — is excluded even if the caller handed it in), labelled,
// minus anything already dismissed. Empty/missing input -> [].
export function retireCandidates(items, dismissedIds, { days = 90, now = Date.now() } = {}) {
  const dismissed = new Set(dismissedIds || []);
  const cutoffMs = now - days * MS_PER_DAY;
  return (items || [])
    .filter((it) => it && !dismissed.has(it.id))
    .filter((it) => {
      const lastUsed = it.last_used_at;
      if (!lastUsed) return true;             // never used -> always a candidate
      const usedMs = Date.parse(lastUsed);
      if (Number.isNaN(usedMs)) return true;   // unparseable -> treat as stale, not fresh
      return usedMs <= cutoffMs;               // fresh (used within the window) excluded
    })
    .map((it) => ({ ...it, label: retireLabel(it, { now }) }));
}
