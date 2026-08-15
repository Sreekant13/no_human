// The Cancel-task confirm flow — plain language, no supervisor ceremony. The
// operator feedback that killed the "Content landed elsewhere?" affordance
// ("not understandable and doesnt help the user, they can just have a cancel
// task button") applies here too: this module is deliberately small — one
// optional reason field, one confirm step, one honest terminal state. The
// landed-override's sha + justification flow stays CLI-only (`nh approve
// --landed`); it is not reachable from here.

export const CANCEL_TITLE = "Cancel this task";
export const CANCEL_BODY =
  "Its record closes as cancelled with the reason you type — no work is marked done.";
export const CANCEL_CONFIRM_LABEL = "Cancel task";
export const CANCEL_KEEP_LABEL = "Keep task";
export const CANCEL_REASON_PLACEHOLDER = "Reason (optional)";

// An unbounded paste into the reason field would bloat `context` forever —
// this is the client-side half of the same 500-char limit the server
// truncates to.
export const REASON_MAX = 500;

export function clampReason(reason) {
  const trimmed = String(reason ?? "").trim();
  if (!trimmed) return null;
  return trimmed.length > REASON_MAX ? trimmed.slice(0, REASON_MAX) : trimmed;
}

// `api` is injected (production: `cancelTask` from api.js) so this stays
// testable without a network — and so the confirm step is provably the only
// path that can reach it: nothing here calls `api` until submitCancel runs.
export async function submitCancel({ taskId, reason, api }) {
  const clamped = clampReason(reason);
  try {
    const res = await api(taskId, clamped);
    return { ok: true, message: res?.message || "Task cancelled." };
  } catch (e) {
    return { ok: false, error: e?.message || "Cancel failed." };
  }
}
