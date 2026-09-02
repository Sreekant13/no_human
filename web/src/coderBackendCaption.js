// Pure view-model for the composer's coder-backend disclosure paragraph.
//
// The picker's "" option IS the config default (TaskComposer.jsx's
// `useState(initial?.backend ?? "")`, sent through as-is so the server falls
// back to worker.backend) — so on "" there is nothing non-default about the
// run and nothing to disclose. Once the user picks an actual backend id, the
// caption discloses the one thing that matters: only the CODER moves, every
// other role stays on Claude (constraint d35aa60e). Roles are always the
// caller's server-derived list (GET /api/config's claude_pinned_roles) —
// this file has no literal role name of its own, so a role added or renamed
// server-side shows up here with no change to this file.
//
// coderBackendCaption(backend, claudePinnedRoles) -> string ("" = render nothing)
export function coderBackendCaption(backend, claudePinnedRoles) {
  const selected = String(backend ?? "").trim();
  if (!selected) return "";
  const roles = Array.isArray(claudePinnedRoles) ? claudePinnedRoles.filter(Boolean) : [];
  if (roles.length > 0) {
    return `Only the coder uses ${selected} — ${roles.join(", ")} stay on Claude.`;
  }
  return `Only the coder uses ${selected}.`;
}

// Which coder backend this run will ACTUALLY use, expressed as "what is worth
// disclosing" — "" means nothing non-default is happening, so no caption.
//   * an explicit pick always wins (the user chose it, so disclose it);
//   * otherwise the server's resolved effective backend counts only when it
//     differs from the server's own pristine default;
//   * an older server that sends neither → "" (pre-change silence, never a
//     guess). This file names no backend and no role: the effective value and
//     the default it is compared against both come from GET /api/config.
export function effectiveCoderBackend(picked, config) {
  const pick = String(picked ?? "").trim();
  if (pick) return pick;
  const effective = String(config?.coder_backend_effective ?? "").trim();
  const fallback = String(config?.coder_backend_default ?? "").trim();
  if (!effective || !fallback) return "";
  return effective.toLowerCase() === fallback.toLowerCase() ? "" : effective;
}
