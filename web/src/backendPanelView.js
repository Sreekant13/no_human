import { titleCase } from "./titleCase.js";

// Pure view-model for the Settings "Models" pane's coder-backend row.
// Given the GET /api/coder-backend payload (see
// core/backend_settings.py::backend_payload), derive everything the row
// renders and everything a Save click sends back. No React, no I/O, no
// backend name / availability / reason string of its own anywhere in this
// file — the option list itself comes from the payload (which in turn comes
// from agent.backend.SUPPORTED_BACKENDS on the server), so a fourth backend
// shows up here with no change to this file.
//
// Payload shape (read, not invented — backend_settings.py::backend_payload):
//   {"current": str, "default": str,
//    "options": [{"id", "available", "reason"}],
//    "restart_required": bool}

// backendPanelView(payload) -> {unavailable, showRestartBanner, current,
//   default, options: [{id, label, disabled, reason, isCurrent}]}
//
// `unavailable` is true for a missing/empty payload (fetchCoderBackend()
// returned null, or an older server answered with a shape this row doesn't
// recognise) — the caller renders a "this server build does not expose…"
// note instead of an empty <select>.
export function backendPanelView(payload) {
  const options = payload?.options;
  if (!Array.isArray(options) || options.length === 0 || !payload.current) {
    return { unavailable: true, showRestartBanner: false, current: "", default: "", options: [] };
  }
  return {
    unavailable: false,
    showRestartBanner: !!payload.restart_required,
    current: payload.current,
    default: payload.default,
    options: options.map((o) => ({
      id: o.id,
      label: titleCase(String(o.id || "")),
      disabled: !o.available,
      reason: o.available ? "" : o.reason || "",
      isCurrent: o.id === payload.current,
    })),
  };
}

// The PUT body for a Save click: `{backend: pending}` when `pending` differs
// from the payload's `current`, or `null` when there is nothing to send —
// the caller should treat `null` as "disable Save", matching the server's
// own no-op-write behaviour for a repeat PUT of the same value.
export function pendingBody(payload, pending) {
  if (!payload || pending === undefined || pending === null) return null;
  if (pending === payload.current) return null;
  return { backend: pending };
}

// A pending selection that is not currently submittable — mirrors
// core/backend_settings.py::apply_backend_change's own "not in
// SUPPORTED_BACKENDS" / "not available" checks so the Save button can be
// disabled AT THE POINT OF CHOICE, using the SAME availability the server
// already computed (payload.options[].available), never a second frontend
// rule that could disagree with it.
export function isSubmittable(payload, pending) {
  const options = payload?.options;
  if (!Array.isArray(options) || !pending) return false;
  const match = options.find((o) => o.id === pending);
  return !!match && !!match.available;
}

// A failed Save (422, network error, or any other throw): the server wrote
// nothing, so the truthful UI reverts the pending edit — `detail` is the
// verbatim BackendSettingsError message (the same reason the dropdown
// itself would already have shown, never a second message).
export function applyError(detail) {
  return { pending: null, error: detail };
}
