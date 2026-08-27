import test from "node:test";
import assert from "node:assert/strict";
import {
  draftFrom, changedValues, toList, listToText, readiness, setupSummary, secretHint,
  humanizeField, switchLabel, effectiveEnabled,
} from "./integrationSetup.js";

// Shaped exactly like GET /api/integrations/setup returns (see
// integrations/__init__.py setup_specs) — fields are already credential-free
// because the server refuses to describe a credential field at all.
const LINEAR = {
  name: "linear", kind: "issue_tracker", enable_field: "enabled",
  enabled: false, enable_default: false, configured: false, detail: "not configured",
  fields: [
    { name: "enabled", label: "Enabled", kind: "bool", value: false },
    { name: "team_key", label: "Team key", kind: "text", value: "" },
    { name: "state_types", label: "State types", kind: "list",
      value: ["triage", "backlog"] },
  ],
  secrets: [{ env_var: "LINEAR_API_KEY", set: false }],
  secret_note: "",
};

// teams.enabled ships True (a mute switch, see effectiveEnabled) — the one
// spec in this file whose raw `enabled` and `enable_default` are both true.
const TEAMS = {
  name: "teams", kind: "notifications", enable_field: "enabled",
  enabled: true, enable_default: true, configured: false, detail: "not configured",
  fields: [{ name: "enabled", label: "Enabled", kind: "bool", value: true }],
  secrets: [],
  secret_note: "The Power Automate webhook URL carries its own credential in " +
    "the query string. Onboarding will not take it — paste it in " +
    "Settings → Integrations → Microsoft Teams.",
};

const JIRA = {
  name: "jira", kind: "issue_tracker", enable_field: "enabled",
  enabled: false, enable_default: false, configured: false, detail: "not configured",
  fields: [{ name: "enabled", label: "Enabled", kind: "bool", value: false }],
  secrets: [{ env_var: "JIRA_API_TOKEN", set: false }],
  secret_note: "",
};

const MONDAY = {
  name: "monday", kind: "issue_tracker", enable_field: "enabled",
  enabled: false, enable_default: false, configured: false, detail: "not configured",
  fields: [{ name: "enabled", label: "Enabled", kind: "bool", value: false }],
  secrets: [{ env_var: "MONDAY_API_TOKEN", set: false }],
  secret_note: "",
};

const SLACK = {
  name: "slack", kind: "chat", enable_field: "intake",
  enabled: false, enable_default: false, configured: false, detail: "not configured",
  fields: [{ name: "intake", label: "Intake", kind: "bool", value: false }],
  secrets: [{ env_var: "SLACK_BOT_TOKEN", set: false }],
  secret_note: "",
};

test("draftFrom seeds one editable value per advertised field", () => {
  assert.deepEqual(draftFrom([LINEAR, TEAMS]), {
    linear: { enabled: false, team_key: "", state_types: ["triage", "backlog"] },
    teams: { enabled: true },
  });
  assert.deepEqual(draftFrom(null), {});
});

test("changedValues emits only fields the user actually changed", () => {
  const draft = draftFrom([LINEAR]);
  assert.deepEqual(changedValues(LINEAR, draft), {});
  draft.linear.enabled = true;
  draft.linear.team_key = "ENG";
  assert.deepEqual(changedValues(LINEAR, draft), { enabled: true, team_key: "ENG" });
});

test("changedValues can never emit a key the server did not advertise", () => {
  // The credential invariant, at the UI layer: even if something planted a
  // secret in the draft, it is not in `fields`, so it is not in the payload.
  const draft = draftFrom([LINEAR]);
  draft.linear.api_key = "lin_api_LEAKME";
  draft.linear.team_key = "ENG";
  const payload = changedValues(LINEAR, draft);
  assert.deepEqual(Object.keys(payload), ["team_key"]);
  assert.ok(!JSON.stringify(payload).includes("LEAKME"));
});

test("list fields round-trip through the comma-string editor", () => {
  assert.deepEqual(toList(" triage , backlog ,, "), ["triage", "backlog"]);
  assert.deepEqual(toList(["a", " b "]), ["a", "b"]);
  assert.equal(listToText(["a", "b"]), "a, b");
  const draft = draftFrom([LINEAR]);
  draft.linear.state_types = "triage, unstarted";
  assert.deepEqual(changedValues(LINEAR, draft), { state_types: ["triage", "unstarted"] });
  // Re-typing the same members in the same order is not a change.
  draft.linear.state_types = "triage, backlog";
  assert.deepEqual(changedValues(LINEAR, draft), {});
});

test("readiness never says ready while the switch is off", () => {
  assert.equal(readiness(LINEAR).state, "off");
  assert.equal(readiness(LINEAR).label, "Off");
});

test("readiness names the missing env var instead of claiming it works", () => {
  const on = { ...LINEAR, enabled: true };
  const r = readiness(on);
  assert.equal(r.state, "needs-key");
  assert.ok(r.label.includes("LINEAR_API_KEY"));
  assert.deepEqual(r.missing, ["LINEAR_API_KEY"]);
});

test("readiness distinguishes 'credential set but settings blank' from ready", () => {
  const keyed = { ...LINEAR, enabled: true, secrets: [{ env_var: "LINEAR_API_KEY", set: true }] };
  assert.equal(readiness(keyed).state, "incomplete");
  // Configured is no longer enough for "ready": a green mark means a live test
  // passed, not that a key was typed. Until then it is "Saved — not verified".
  assert.equal(readiness({ ...keyed, configured: true }).state, "saved-unverified");
  assert.equal(readiness({ ...keyed, configured: true }, { verified: true }).state, "ready");
});

test("readiness is 'Saved — not verified' (warn) until a connection test passes", () => {
  const configured = { ...LINEAR, enabled: true, configured: true,
    secrets: [{ env_var: "LINEAR_API_KEY", set: true }] };
  const unverified = readiness(configured, { verified: false });
  assert.equal(unverified.state, "saved-unverified");
  assert.equal(unverified.label, "Saved — not verified");
  assert.equal(unverified.tone, "warn");
  const verified = readiness(configured, { verified: true });
  assert.equal(verified.state, "ready");
  assert.equal(verified.label, "Ready");
  assert.equal(verified.tone, "ok");
});

test("readiness reads the server's persisted verified flag when no opt is passed", () => {
  const configured = { ...LINEAR, enabled: true, configured: true,
    secrets: [{ env_var: "LINEAR_API_KEY", set: true }] };
  // spec.verified (from integrations.<name>.last_verified_at) counts as a pass.
  assert.equal(readiness({ ...configured, verified: true }).state, "ready");
  assert.equal(readiness({ ...configured, verified: false }).state, "saved-unverified");
});

test("an integration with no switch is never reported off", () => {
  const noSwitch = { ...TEAMS, enable_field: null, enabled: null, configured: true, verified: true };
  assert.equal(readiness(noSwitch).state, "ready");
  // Without a passing test it is saved-unverified, still never "off".
  assert.equal(readiness({ ...noSwitch, verified: false }).state, "saved-unverified");
});

test("setupSummary counts what is on and what is actually ready", () => {
  const ready = { ...TEAMS, configured: true, verified: true };
  assert.deepEqual(setupSummary([LINEAR, ready]), { total: 2, on: 1, ready: 1 });
  // A configured-but-unverified integration is on, but not yet ready.
  assert.deepEqual(setupSummary([LINEAR, { ...TEAMS, configured: true }]),
                   { total: 2, on: 1, ready: 0 });
  assert.deepEqual(setupSummary([]), { total: 0, on: 0, ready: 0 });
});

test("secretHint names the env var and says config.yaml is not the place", () => {
  const hint = secretHint(LINEAR);
  assert.ok(hint.includes("LINEAR_API_KEY"));
  assert.ok(hint.includes("~/.no_human/.env"));
  assert.ok(hint.includes("never in config.yaml"));
});

test("secretHint uses the server's note when there is one (Teams)", () => {
  assert.equal(secretHint(TEAMS), TEAMS.secret_note);
  assert.ok(secretHint(TEAMS).includes("Settings"));
  // And it must not invent an env var Teams does not have.
  assert.ok(!secretHint(TEAMS).includes(".env"));
});

test("the switch label names what it actually switches", () => {
  assert.equal(switchLabel(LINEAR, "Linear"), "Enable Linear");
  // Slack's switch is `intake` (the Socket-Mode worker), which connects only —
  // the mention-to-task handler isn't wired — so the label states what it does
  // now and what's coming, without promising intake or "Enable Slack".
  const slack = { name: "slack", enable_field: "intake", fields: [], secrets: [] };
  assert.equal(switchLabel(slack, "Slack"), "Connect Slack — task intake coming soon");
  assert.equal(switchLabel({ enable_field: null }, "GitHub"), "");
  assert.equal(humanizeField("poll_interval"), "poll interval");
});

test("secretHint is honest when nothing is needed", () => {
  assert.equal(secretHint({ name: "x", secrets: [], secret_note: "" }),
               "No credential needed.");
});

test("a mute switch that ships on reads Off until the channel is configured", () => {
  const r = readiness(TEAMS);
  assert.equal(r.label, "Off");
  assert.equal(r.tone, "neutral");
  assert.equal(effectiveEnabled(TEAMS, { enabled: true }), false);
});

test("a fresh-config wizard shows every integration off", () => {
  const fresh = [JIRA, LINEAR, MONDAY, SLACK, TEAMS];
  for (const spec of fresh) {
    assert.equal(readiness(spec).state, "off", `${spec.name} should read off`);
    assert.equal(effectiveEnabled(spec), false, `${spec.name} should be effectively off`);
  }
  assert.equal(setupSummary(fresh).on, 0);
});

test("an opt-in switch the user turned on still reads On — needs settings", () => {
  const on = { ...JIRA, enabled: true, enable_default: false,
    secrets: [{ env_var: "JIRA_API_TOKEN", set: true }] };
  assert.equal(readiness(on).label, "On — needs settings");
});

test("a configured mute switch stays on", () => {
  const configured = { ...TEAMS, configured: true };
  assert.notEqual(readiness(configured).state, "off");
});
