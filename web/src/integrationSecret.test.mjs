import { test } from "node:test";
import assert from "node:assert/strict";
import { secretState, fieldSecretLabel, secretFields } from "./integrationSecret.js";

// Class audit (E2E walk 2026-08-12): the card's Secret summary line used to
// render `it.configured` — an integration-wide predicate computed from
// non-secret settings (monday: board_id AND status_column) — while the
// Configure form's badge/placeholder read the secret field's own `set`. One
// card, two answers, both visible at once.
//
// Divergent both directions (summary could say either thing regardless of
// the actual credential): jira, linear, monday, circleci.
// Coincidentally agreed (their only field IS the webhook, so `configured`
// happens to equal the secret's own state): slack, teams.
// No Secret line at all today (no SECRET_ENV_KEY entry): github, gitlab.
// jenkins has a SECRET_ENV_KEY entry pointing at JENKINS_API_TOKEN, but two
// .env secrets (JENKINS_USER + JENKINS_API_TOKEN) — found, not fixed here;
// `secretState` still derives correctly for it (requires BOTH `set`).
//
// This module is the fix: one derivation, called from both the summary line
// and the form badge/placeholder, so they cannot disagree again.

const apiTokenField = (set) => ({ name: "api_token", label: "API token", secret: true, set });

test("summary and form agree when the secret is NOT set", () => {
  const it = {
    name: "monday",
    configured: false,
    fields: [
      { name: "board_id", label: "Board id", secret: false, set: false },
      { name: "status_column", label: "Status column id", secret: false, set: false },
      apiTokenField(false),
    ],
  };
  const field = it.fields.find((f) => f.name === "api_token");
  assert.equal(secretState(it).label, "not set");
  assert.equal(fieldSecretLabel(field), "not set");
  assert.equal(secretState(it).label, fieldSecretLabel(field));
});

test("summary and form agree when the secret IS set", () => {
  const it = {
    name: "monday",
    configured: false,
    fields: [
      { name: "board_id", label: "Board id", secret: false, set: false },
      { name: "status_column", label: "Status column id", secret: false, set: false },
      apiTokenField(true),
    ],
  };
  const field = it.fields.find((f) => f.name === "api_token");
  assert.equal(secretState(it).label, "●●● set");
  assert.equal(fieldSecretLabel(field), "●●● set");
  assert.equal(secretState(it).label, fieldSecretLabel(field));
});

test("the reported monday contradiction cannot recur", () => {
  // (a) token set, board/column blank — configured is false, but the
  // credential IS present: the summary must no longer track `configured`.
  const tokenSetNotConfigured = {
    name: "monday",
    configured: false,
    fields: [
      { name: "board_id", label: "Board id", secret: false, set: false },
      { name: "status_column", label: "Status column id", secret: false, set: false },
      apiTokenField(true),
    ],
  };
  const stateA = secretState(tokenSetNotConfigured);
  assert.equal(stateA.label, "●●● set");
  assert.equal(fieldSecretLabel(tokenSetNotConfigured.fields[2]), "●●● set");
  assert.notEqual(stateA.set, tokenSetNotConfigured.configured);

  // (b) inverse: board+column set (configured true), token absent.
  const configuredTokenAbsent = {
    name: "monday",
    configured: true,
    fields: [
      { name: "board_id", label: "Board id", secret: false, set: true },
      { name: "status_column", label: "Status column id", secret: false, set: true },
      apiTokenField(false),
    ],
  };
  const stateB = secretState(configuredTokenAbsent);
  assert.equal(stateB.label, "not set");
  assert.equal(fieldSecretLabel(configuredTokenAbsent.fields[2]), "not set");
});

// Mirrors FIELD_SPECS in src/no_human/integrations/__init__.py (secret flags
// only — non-secret fields are irrelevant to secretState/fieldSecretLabel).
const ALL_NINE = {
  jira: [{ name: "api_token", secret: true }],
  linear: [{ name: "api_key", secret: true }],
  monday: [{ name: "api_token", secret: true }],
  circleci: [{ name: "api_token", secret: true }],
  github: [], // no secret field — no SECRET_ENV_KEY entry, no Secret line
  gitlab: [], // no secret field — no SECRET_ENV_KEY entry, no Secret line
  jenkins: [{ name: "user", secret: true }, { name: "api_token", secret: true }],
  slack: [{ name: "webhook_url", secret: true }],
  teams: [{ name: "webhook_url", secret: true }],
};

test("all nine integration cards agree between summary and form", () => {
  for (const [name, secretSpecs] of Object.entries(ALL_NINE)) {
    for (const allSet of [true, false]) {
      const fields = secretSpecs.map((s) => ({ ...s, label: s.name, set: allSet }));
      const it = { name, configured: allSet, fields };
      const state = secretState(it);
      if (secretSpecs.length === 0) {
        assert.equal(state, null, `${name} advertises no secret field — summary must render nothing`);
        continue;
      }
      for (const f of secretFields(it)) {
        assert.equal(
          state.label, fieldSecretLabel(f),
          `${name}: summary label must equal every secret field's own label`,
        );
      }
    }
  }
});

test("missing or malformed field data never reports \"set\"", () => {
  assert.equal(secretState({}), null);
  assert.equal(secretState({ fields: [] }), null);
  assert.equal(secretState({ fields: [{ name: "api_token", secret: true }] }).label, "not set");
  assert.equal(secretState({ fields: [{ secret: true, set: "yes" }] }).label, "not set");
  assert.equal(fieldSecretLabel({ name: "api_token", secret: true }), "not set");
  assert.equal(fieldSecretLabel({ secret: true, set: "yes" }), "not set");
  assert.equal(fieldSecretLabel(undefined), "not set");
  assert.equal(fieldSecretLabel(null), "not set");
});
