import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// The wizard's "Connect your tools" step. Static source analysis, the same way
// integrations.test.mjs / onboardingRoster.test.mjs read their JSX — no
// jsdom/React renderer is wired into this project's `node --test` harness.
//
// The step used to render a read-only status list: grepping it for
// linear/teams/slack/jira returned only `<linearGradient>`, an SVG tag. These
// assertions hold the two properties that fixed that, and the one that must
// never regress: the card family contains NO credential input.

const here = fileURLToPath(new URL(".", import.meta.url));
const src = readFileSync(here + "Onboarding.jsx", "utf8");

// The card component, isolated so assertions can't accidentally match JSX from
// another step (the repos step has text inputs of its own).
const CARD = (() => {
  const start = src.indexOf("function IntegrationSetupCard");
  assert.ok(start > 0, "IntegrationSetupCard must exist");
  const end = src.indexOf("\nfunction Stagger", start);
  assert.ok(end > start, "could not bound IntegrationSetupCard");
  return src.slice(start, end);
})();

const STEP = (() => {
  const start = src.indexOf('{step.key === "integrations" &&');
  assert.ok(start > 0, "the integrations step must exist");
  const end = src.indexOf('{step.key === "history" &&', start);
  assert.ok(end > start, "could not bound the integrations step");
  return src.slice(start, end);
})();

test("the step is generated from the server's discovered specs", () => {
  // Fetched from the setup endpoint (which walks DEFAULT_CONFIG), not the
  // status-only endpoint the read-only version used.
  assert.match(src, /fetchIntegrationSetup\(\)/);
  assert.match(STEP, /integrations\.map\(\(spec\)/);
  assert.match(STEP, /<IntegrationSetupCard/);
});

test("no integration is named in the UI — a new one renders with no edit here", () => {
  // The bug report was "grep finds only <linearGradient>". The fix must not be
  // "now grep finds four hardcoded names".
  for (const name of ["jira", "linear", "circleci", "slack", "teams"]) {
    assert.ok(
      !new RegExp(`["'\`]${name}["'\`]`, "i").test(STEP + CARD),
      `${name} must not be hardcoded in the integrations step`,
    );
  }
});

test("the on/off switch is whatever the spec's enable_field names", () => {
  assert.match(CARD, /const enableField = spec\.enable_field;/);
  // The checkbox reflects EFFECTIVE state, not the raw stored value: a mute
  // switch that ships on (e.g. teams) must not render as checked on a fresh,
  // unconfigured install — see effectiveEnabled() in integrationSetup.js.
  assert.match(CARD, /checked=\{isOn\}/);
  assert.match(CARD, /effectiveEnabled\(spec, values\)/);
});

test("every other control comes from the spec's fields, typed by `kind`", () => {
  assert.match(CARD, /settings\.map\(\(f\)/);
  assert.match(CARD, /f\.kind === "bool"/);
  assert.match(CARD, /f\.kind === "list"/);
  assert.match(CARD, /\{f\.label\}/);
});

test("THE CREDENTIAL RULE: the card renders no secret input of any kind", () => {
  // A password field, a `secret` branch, or a literal token/key input in this
  // family would mean the wizard could carry a credential into config.yaml.
  assert.ok(!/type="password"/.test(CARD), "no password input");
  assert.ok(!/type=\{[^}]*password/.test(CARD), "no conditional password input");
  assert.ok(!/f\.secret/.test(CARD), "the setup spec has no `secret` field to branch on");
  assert.ok(!/api_key|api_token|webhook_url|\bsecret_value\b/i.test(CARD),
            "no credential field name may appear in the card");
  // What it does instead: name the env var.
  assert.match(CARD, /secretHint\(spec\)/);
});

test("the step tells the user where credentials go, in the step copy itself", () => {
  assert.match(STEP, /~\/\.no_human\/\.env/);
  assert.match(STEP, /never taken here/i);
});

test("the readiness chip comes from the server's spec, not from the draft", () => {
  // Otherwise the card would claim "Ready" for something that was typed but
  // never written — the exact "Settings disagrees with reality" defect.
  // C2: readiness now takes the verified flag (a passing live test / persisted
  // last_verified_at) so "Ready" means the connection works, not that a value
  // was typed. Still driven by the server spec, never the draft.
  assert.match(CARD, /const ready = readiness\(spec, \{ verified \}\);/);
  assert.match(CARD, /integration-chip tone-\$\{ready\.tone\}/);
});

test("the Launch summary counts integrations from the server specs", () => {
  const summary = src.slice(src.indexOf('{step.key === "summary" &&'));
  assert.match(summary, /setupSummary\(integrations\)/);
  assert.match(summary, /Integrations on/);
});

test("saving sends only changed values and re-seeds from the response", () => {
  const save = src.slice(src.indexOf("async function saveIntegration"),
                         src.indexOf("function toggleRepo"));
  assert.match(save, /changedValues\(spec, intDraft\)/);
  assert.match(save, /saveIntegrationSetup\(spec\.name, values\)/);
  // The refreshed spec replaces the local one, so the card reflects what
  // landed on disk rather than what was typed.
  assert.match(save, /draftFrom\(\[refreshed\]\)\[spec\.name\]/);
});

test("every class the card uses has a rule in styles.css", () => {
  const css = readFileSync(here + "styles.css", "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  const used = new Set();
  for (const m of CARD.matchAll(/className="([^"{]+)"/g)) {
    for (const c of m[1].split(/\s+/)) if (c.startsWith("ob-integration")) used.add(c);
  }
  assert.ok(used.size >= 8, `expected the card to use its own classes, saw ${used.size}`);
  for (const c of used) {
    assert.ok(new RegExp(`\\.${c}[\\s,.:{]`).test(css), `.${c} has no rule in styles.css`);
  }
});

test("default_repo renders as a select over registered repos, not free text (C3)", () => {
  // "Run tasks in repo" must be a dropdown over the operator's registered
  // profiles — a pulled-in ticket can only ever name a repo no_human knows.
  assert.match(CARD, /f\.kind === "repo_select"/);
  assert.match(CARD, /\(f\.options \|\| \[\]\)\.map\(\(opt\)/);
  assert.match(CARD, /<select/);
});

test("the Integrations step explains that GitHub/GitLab/CI come from the repo profile (C3)", () => {
  assert.match(STEP, /configured per repository from its profile/);
  assert.match(STEP, /GitHub, GitLab and CI/);
});
