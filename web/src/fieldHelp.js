// Pure helpers for the FieldHint renderer, kept out of the JSX so `node --test`
// can exercise them. The help TEXT and URL are never hard-coded here — they
// arrive from the server (integrations/help.py, via setup_specs /
// integration_fields), so a new field's help lands with no web change.

// A stable DOM id for one field's hint <p>, so the input can point at it with
// aria-describedby. `hint-linear-team_key`.
export function hintId(integration, field) {
  return `hint-${integration}-${field}`;
}

// The bare host of a help URL, for the "Open <host> ↗" link label. Never
// throws: a blank or non-URL string just yields "" and the link renders
// without a host label.
export function hostLabel(url) {
  try {
    return new URL(url).host;
  } catch {
    return "";
  }
}
