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
// worker, which today CONNECTS ONLY: the mention-to-task intake handler is not
// wired in `nh serve`, so "Enable Slack intake" would promise the wrong thing
// in the opposite direction. It reads "Enable Slack worker (connect only)"
// until the handler is wired.
export function switchLabel(spec, name) {
  if (!spec.enable_field) return "";
  if (spec.enable_field === "intake")
    // Connect-only until the mention-to-task handler is wired in `nh serve` —
    // see the comment above; the label must not promise intake.
    return `Enable ${name} worker (connect only)`;
  return spec.enable_field === "enabled"
    ? `Enable ${name}`
    : `Enable ${name} ${humanizeField(spec.enable_field)}`;
}

// Whether an integration's switch is effectively ON, as opposed to what its
// raw config value says. A switch that SHIPS ON (`enable_default: true`, e.g.
// teams) is a mute switch — it exists so an upgrade never silently mutes an
// already-pasted webhook, not a claim that the channel is live. With no
// webhook the notifier no-ops regardless. On a fresh install, presenting that
// raw `true` next to every other integration's `false` reads as "this one is
// somehow already on". So: a mute switch counts as on only once the
// integration is actually `configured`; a switch that ships OFF is a plain
// opt-in, and if it is on, the user turned it on themselves.
//
// `values`, when given, is the wizard's draft for this integration
// ({fieldName: value}, see draftFrom); omitted, this falls back to the raw
// spec's own `enabled`. Presentation only — nothing here changes what is
// stored or what notify.build_notifier reads, so upgrade behaviour (an
// existing install with a pasted webhook) is untouched.
export function effectiveEnabled(spec, values) {
  const field = spec.enable_field;
  if (!field) return true; // no switch at all (ci.* views) — never "off"
  const raw = (values || {})[field] ?? spec.enabled;
  if (raw === false) return false;
  return !(spec.enable_default === true && !spec.configured);
}

// The honest state line for one integration — the whole point of the step is
// that this cannot say "Configured" while nothing runs.
//
// Four states, in the order they are decided:
//   off        — its own switch is effectively off (see effectiveEnabled)
//   needs-key  — switched on, but a credential it needs is not in .env yet
//   incomplete — switched on and credentialled, but its settings are blank
//   ready      — on, credentialled and configured
// An integration with no switch (enable_field null) is never "off".
//
// KNOWN IMPRECISION, in the safe direction. `configured` is the registry's own
// predicate for the integration as a whole, which for Slack means "a notify
// webhook is set" — not "the intake worker can run". So Slack with `intake` on
// and both Socket-Mode tokens in .env, but no notify webhook, reads
// "On — needs settings" when the worker would in fact connect. It under-reports
// readiness; it can never report ready for something that is not, which is the
// direction that matters when the whole point of this step is to stop the UI
// claiming an integration is live while nothing runs.
export function readiness(spec) {
  const missing = (spec.secrets || []).filter((s) => !s.set).map((s) => s.env_var);
  if (spec.enable_field && !effectiveEnabled(spec)) {
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
