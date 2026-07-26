// SCRUM-67 3/3: the poll→state wiring for the header's drain chip, kept pure
// (and node --test'able) the same way tasksReducer.js/nightLedger.js are —
// App.jsx just calls nextDrainReadout on every poll outcome and passes
// readoutPayload(state) into drainChip().
//
// Binding review carry-over from SCRUM-70/71: never render the chip before
// the FIRST successful health fetch (a pending state would otherwise format
// as "0/0 workers busy" — a phantom number that looks live), and once a
// payload has arrived, a later poll failure must show the honest unreachable
// state, never the last-known numbers frozen in place.
export const initialDrainReadout = { status: "pending", data: null };

// outcome: the resolved poll result — a payload object on success, `null`
// when the endpoint is gated as unsupported (queueHealthGate 404), or the
// string "error" when the fetch rejected (network/5xx).
export function nextDrainReadout(state, outcome) {
  if (outcome === "error") {
    // Before any success, a failure is still "no data yet", not "unreachable"
    // — the operator hasn't been told this feature exists yet.
    return state.status === "pending" ? state : { status: "error", data: null };
  }
  if (outcome == null) return state; // unsupported/no-op poll: leave as-is
  return { status: "ok", data: outcome };
}

// The value to feed drainChip(), or null to render nothing at all.
export function readoutPayload(state) {
  if (state.status === "ok") return state.data;
  if (state.status === "error") return { error: true };
  return null;
}
