// The ONE derivation of "is this integration's credential set?".
// Both the card's Secret summary line and the Configure form's badge/
// placeholder call this. They used to disagree: the summary rendered
// `it.configured` — the integration-WIDE predicate (monday: board_id AND
// status_column, which never reads MONDAY_API_TOKEN) — while the form
// rendered the api_token field's own `set`. One card, two answers.
export const SECRET_SET_TEXT = "●●● set";
export const SECRET_UNSET_TEXT = "not set";

export function secretFields(integration) {
  return ((integration && integration.fields) || []).filter((f) => f.secret);
}

// null  → this integration advertises no secret field; render no claim.
// {set} → every advertised secret is present (`set: true` from the API).
export function secretState(integration) {
  const secrets = secretFields(integration);
  if (secrets.length === 0) return null;
  const set = secrets.every((f) => f.set === true);
  return { set, label: set ? SECRET_SET_TEXT : SECRET_UNSET_TEXT };
}

// The same two words for ONE field's badge/placeholder, so the form row and
// the summary line cannot drift apart in wording either.
export function fieldSecretLabel(field) {
  return field && field.set === true ? SECRET_SET_TEXT : SECRET_UNSET_TEXT;
}
