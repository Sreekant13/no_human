// Version-skew tolerance (live finding 2026-07-18): the operator's
// long-running daemon can be OLDER than the bundle it serves — its missing
// endpoints 404 on every poll for the whole session (observed: 6×/min
// console noise on /api/queue/health across every flow). A 404 means "this
// server has no such endpoint": remember it and stop asking. null = feature
// unavailable; the alarm chip is null-conditional and stays hidden.
export function makeEndpointGate(doFetch) {
  let unsupported = false;
  return async function gated() {
    if (unsupported) return null;
    const r = await doFetch();
    if (r.status === 404) { unsupported = true; return null; }
    if (!r.ok) throw new Error("queue health failed");
    return r.json();
  };
}
