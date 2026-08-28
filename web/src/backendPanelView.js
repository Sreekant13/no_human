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
    localFields: localFields(payload),
    options: options.map((o) => ({
      id: o.id,
      label: titleCase(String(o.id || "")),
      disabled: !o.available,
      reason: o.available ? "" : o.reason || "",
      isCurrent: o.id === payload.current,
    })),
  };
}

// The backend whose config fields the row can set inline (a model id + a
// loopback URL), and the fields themselves. Everything — the backend id, the
// config-key each field PUTs under, its label/placeholder and current value —
// comes from the payload, so this file names no backend id or config key of
// its own (same rule the option list follows). Returns null for a server build
// that does not send them, so the row renders no extra fields.
//
// Shape (server-owned, backend_settings.backend_payload):
//   {backend, fields: [{key, value, label, placeholder}]}
export function localFields(payload) {
  const lf = payload && payload.local_fields;
  if (!lf || typeof lf.backend !== "string" || !Array.isArray(lf.fields)) return null;
  return {
    backend: lf.backend,
    fields: lf.fields.map((f) => ({
      key: f.key,
      value: f.value || "",
      label: f.label || f.key,
      placeholder: f.placeholder || "",
    })),
  };
}

// Which backend the dropdown currently points at (the pending edit if any,
// else the payload's current). One definition, reused by the show/submit/gate
// helpers so they can never disagree on what "selected" means.
function selectedBackend(payload, pending) {
  return pending === null || pending === undefined ? (payload && payload.current) : pending;
}

// Whether the row should show the local config fields: the selected backend is
// the one `localFields` names.
export function showLocalFields(payload, pending) {
  const lf = localFields(payload);
  return !!lf && selectedBackend(payload, pending) === lf.backend;
}

// The full PUT body for a Save: the backend change (via `pendingBody`) PLUS any
// local field that differs from what the server reported, but only when the
// selected backend is the local one. `values` maps a field key -> the current
// input string. Returns null when there is nothing to send (Save disabled),
// matching the server's own no-op-write behaviour for a repeat PUT.
export function submitBody(payload, pending, values) {
  if (!payload) return null;
  const body = {};
  const backendPart = pendingBody(payload, pending);
  if (backendPart) Object.assign(body, backendPart);
  const lf = localFields(payload);
  if (lf && selectedBackend(payload, pending) === lf.backend) {
    for (const f of lf.fields) {
      const v = String((values && values[f.key]) ?? f.value).trim();
      if (v !== f.value) body[f.key] = v;
    }
  }
  return Object.keys(body).length ? body : null;
}

// Whether Save may fire. Extends `isSubmittable` (backend availability) with
// the local-fields rule: when the selected backend is the local one, EVERY
// field must be non-blank, and its own availability is NOT required — the
// operator is configuring it right now, and the server runs the real
// post-write preflight. For any other pending backend the availability gate is
// unchanged.
export function canSubmit(payload, pending, values) {
  if (!submitBody(payload, pending, values)) return false;
  const lf = localFields(payload);
  if (lf && selectedBackend(payload, pending) === lf.backend) {
    return lf.fields.every(
      (f) => String((values && values[f.key]) ?? f.value).trim() !== "",
    );
  }
  return pending === null || pending === undefined || isSubmittable(payload, pending);
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
