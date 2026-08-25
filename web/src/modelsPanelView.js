import { titleCase } from "./titleCase.js";

// Pure view-model for the Settings "Models" pane (model picker part 3 of 3).
// Given the GET /api/models payload (see core/model_settings.py::models_payload),
// derive everything the pane renders and everything a Save/Reset click sends
// back. No React, no I/O, no model id / price / default / rule string of its
// own anywhere in this file — every value a row shows comes from the payload
// the server just sent; this module only reshapes it.
//
// Payload shape (read, not invented — model_settings.py::models_payload):
//   {"roles": [{"role","key","current","default",
//               "options":[{"id","price_class":{"label","input_rate",
//                           "output_rate"},"is_default","note",
//                           "requires_backend","disabled_reason"}],
//               "cost_note"}],
//    "restart_required": bool}

// modelsPanelView(payload) -> {unavailable, showRestartBanner, rows}
//
// `unavailable` is true for a missing/empty payload (fetchModels() returned
// null, or an older server answered with a shape this pane doesn't
// recognise) — the caller renders the "this server build does not expose…"
// note instead of five empty rows.
export function modelsPanelView(payload) {
  const roles = payload?.roles;
  if (!Array.isArray(roles) || roles.length === 0) {
    return { unavailable: true, showRestartBanner: false, rows: [] };
  }
  const rows = roles.map((r) => ({
    role: r.role,
    key: r.key,
    label: titleCase(String(r.role || "")),
    current: r.current,
    default: r.default,
    note: r.note || "",
    costNote: r.cost_note || "",
    options: (r.options || []).map((o) => ({
      id: o.id,
      priceLabel: o.price_class?.label ?? "",
      disabled: !!o.requires_backend,
      reason: o.disabled_reason || "",
      isDefault: !!o.is_default,
    })),
  }));
  return {
    unavailable: false,
    showRestartBanner: !!payload.restart_required,
    rows,
  };
}

// The PUT body for a Save click: only the keys whose pending selection
// differs from the row's `current` value. `pending` is `{config_key:
// model_id}` for every row the user has touched (Reset never needs this —
// see resetBody below), keyed by `row.key` exactly as the payload spells it.
export function pendingBody(payload, pending) {
  const roles = payload?.roles;
  if (!Array.isArray(roles) || !pending) return {};
  const out = {};
  for (const r of roles) {
    const next = pending[r.key];
    if (next === undefined) continue;
    if (next === r.current) continue;
    out[r.key] = next;
  }
  return out;
}

// The PUT body for a Reset-to-defaults click: every role's `default` value,
// filtered to the ones that actually differ from `current` — an idempotent
// PUT (Reset when everything is already at its default) sends an empty body,
// which the server treats as a no-op write (no event, nothing on disk).
export function resetBody(payload) {
  const roles = payload?.roles;
  if (!Array.isArray(roles)) return {};
  const out = {};
  for (const r of roles) {
    if (r.default !== r.current) out[r.key] = r.default;
  }
  return out;
}

// A failed Save (422, network error, or any other throw): the server wrote
// nothing (apply_model_changes validates every submitted key before writing
// any of them), so the truthful UI reverts every pending edit, not just the
// one field a heuristic might guess is at fault — `detail` is a single
// unattributed string that names no field.
export function applyError(_pending, detail) {
  return { pending: {}, error: detail };
}
