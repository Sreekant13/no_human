// Recency helpers for the onboarding Repositories step. GET /api/repos/discover
// already returns rows newest-first, each with an `mtime` (epoch seconds, or
// null when the repo has no readable git metadata). These turn that into a
// "Recent" pick-grid at the top of the step and a relative-time label, so a
// user with dozens of repos reaches the one they were just in first.

// The newest repos — at most `limit`, and no older than `withinDays` — split
// off from the rest. `repos` is assumed already newest-first (the server sorts
// it); `rest` keeps that order, so the full list below the cards stays stable.
export function splitRecent(repos, nowEpoch, { limit = 6, withinDays = 30 } = {}) {
  const list = Array.isArray(repos) ? repos : [];
  const cutoff = nowEpoch - withinDays * 86400;
  const recent = [];
  const rest = [];
  for (const r of list) {
    if (recent.length < limit && typeof r?.mtime === "number" && r.mtime >= cutoff) {
      recent.push(r);
    } else {
      rest.push(r);
    }
  }
  return { recent, rest };
}

// "2h ago" / "3d ago" / "just now", or "—" when the row carries no mtime (a
// repo whose git metadata could not be read — saying "0s ago" would be a lie).
export function relativeMtime(mtime, nowEpoch) {
  if (typeof mtime !== "number") return "—";
  const s = Math.max(0, nowEpoch - mtime);
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(s / 3600);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

// Trailing-edge debounce: the wrapped fn runs `ms` after the LAST call. No
// reusable one existed in web/src (composerDraft/Stats each inline their own);
// the folder-scan-as-you-type in the repos step is the second site, so it earns
// a shared one. `.cancel()` clears a pending call (used on unmount).
export function debounce(fn, ms) {
  let t;
  const wrapped = (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
  wrapped.cancel = () => clearTimeout(t);
  return wrapped;
}
