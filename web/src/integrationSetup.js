// Pure logic for the onboarding "Connect your tools" step. Kept out of the JSX
// so it is unit-testable with `node --test`, and so the wizard's diff stays
// small.
//
// EVERYTHING here is driven by the spec objects GET /api/integrations/setup
// returns — one per block under DEFAULT_CONFIG["integrations"]. Nothing in
// this file names an integration, so a sixth block renders with no edit here
// or in Onboarding.jsx.
//
// The one invariant these helpers exist to preserve: a secret is NEVER part of
// a draft or a payload. The server refuses credential fields (422), but the UI
// must not offer one in the first place — `fields` from the API is already
// credential-free, and `changedValues` only ever emits keys that came from it.

// The initial editable draft: {integrationName: {fieldName: value}}.
export function draftFrom(specs) {
  const out = {};
  for (const s of specs || []) {
    const fields = {};
    for (const f of s.fields || []) fields[f.name] = f.value;
    out[s.name] = fields;
  }
  return out;
}

function sameValue(a, b) {
  if (Array.isArray(a) || Array.isArray(b)) {
    const x = Array.isArray(a) ? a : [];
    const y = Array.isArray(b) ? b : [];
    return x.length === y.length && x.every((v, i) => v === y[i]);
  }
  return a === b;
}

// The PUT payload for one integration: only fields the user actually changed,
// and only fields the server advertised. An unknown key can never be
// introduced here, so a credential can never be smuggled into config.yaml
// through the wizard.
export function changedValues(spec, draft) {
  const out = {};
  const current = (draft || {})[spec.name] || {};
  for (const f of spec.fields || []) {
    if (!(f.name in current)) continue;
    // A list field is EDITED as a comma string but ARRIVES as an array, so
    // both sides have to be normalised before "did this change?" can be
    // asked — comparing the raw forms reported every list as dirty on open.
    const next = f.kind === "list" ? toList(current[f.name]) : current[f.name];
    const was = f.kind === "list" ? toList(f.value) : f.value;
    if (sameValue(next, was)) continue;
    out[f.name] = next;
  }
  return out;
}

// A "list" field is edited as a comma-separated string; normalise either form.
export function toList(value) {
  if (Array.isArray(value)) return value.map((v) => String(v).trim()).filter(Boolean);
  return String(value ?? "").split(",").map((v) => v.trim()).filter(Boolean);
}

export function listToText(value) {
  return Array.isArray(value) ? value.join(", ") : String(value ?? "");
}

// `poll_interval` → `poll interval`. Used only for the on/off switch's label.
export function humanizeField(field) {
  return String(field ?? "").replace(/[-_]+/g, " ").trim();
}

// What the on/off switch says. `enabled` is the ordinary case and reads
// "Enable Linear". Slack's switch is `intake` — it starts the Socket-Mode
// intake worker and has nothing to do with Slack notifications — so naming it
// "Enable Slack" would promise the wrong thing; it reads "Enable Slack intake".
export function switchLabel(spec, name) {
  if (!spec.enable_field) return "";
  return spec.enable_field === "enabled"
    ? `Enable ${name}`
    : `Enable ${name} ${humanizeField(spec.enable_field)}`;
}

// The honest state line for one integration — the whole point of the step is
// that this cannot say "Configured" while nothing runs.
//
// Four states, in the order they are decided:
//   off        — its own switch is off, so nothing runs whatever else is set
//   needs-key  — switched on, but a credential it needs is not in .env yet
//   incomplete — switched on and credentialled, but its settings are blank
//   ready      — on, credentialled and configured
// An integration with no switch (enable_field null) is never "off".
//
// KNOWN IMPRECISION, in the safe direction. `configured` is the registry's own
// predicate for the integration as a whole, which for Slack means "a notify
// webhook is set" — not "the intake worker can run". So Slack with `intake` on
// and both Socket-Mode tokens in .env, but no notify webhook, reads
// "On — needs settings" when intake would in fact work. It under-reports
// readiness; it can never report ready for something that is not, which is the
// direction that matters when the whole point of this step is to stop the UI
// claiming an integration is live while nothing runs.
export function readiness(spec) {
  const missing = (spec.secrets || []).filter((s) => !s.set).map((s) => s.env_var);
  if (spec.enable_field && spec.enabled === false) {
    return { state: "off", label: "Off", tone: "neutral", missing };
  }
  if (missing.length > 0) {
    return {
      state: "needs-key", tone: "warn", missing,
      label: `On — set ${missing.join(" and ")} in ~/.no_human/.env`,
    };
  }
  if (!spec.configured) {
    return { state: "incomplete", label: "On — needs settings", tone: "warn", missing };
  }
  return { state: "ready", label: "Ready", tone: "ok", missing };
}

// Summary counts for the wizard's last step, so "Launch" agrees with what was
// actually written.
export function setupSummary(specs) {
  let on = 0;
  let ready = 0;
  for (const s of specs || []) {
    const r = readiness(s);
    if (r.state !== "off") on += 1;
    if (r.state === "ready") ready += 1;
  }
  return { total: (specs || []).length, on, ready };
}

// Where this integration's credential goes, in one sentence. Never a value —
// the API only ever sends env-var NAMES.
export function secretHint(spec) {
  if (spec.secret_note) return spec.secret_note;
  const vars = (spec.secrets || []).map((s) => s.env_var);
  if (vars.length === 0) return "No credential needed.";
  return `Put ${vars.join(" and ")} in ~/.no_human/.env — never in config.yaml.`;
}
