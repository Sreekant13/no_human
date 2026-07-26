import test from "node:test";
import assert from "node:assert/strict";
import { statusChip, KIND_LABEL, BRAND_COLOR, NAME_LABEL, CONFIG_HINT } from "./integrationChip.js";

const ALL = ["jira", "github", "gitlab", "jenkins", "circleci", "slack"];

test("statusChip maps the four non-ambient states", () => {
  assert.deepEqual(statusChip({ configured: false }), { label: "Unconfigured", tone: "neutral" });
  assert.deepEqual(statusChip({ configured: true, healthy: null }), { label: "Configured", tone: "ok" });
  assert.deepEqual(statusChip({ configured: true, healthy: true }), { label: "Connected", tone: "ok" });
  assert.deepEqual(statusChip({ configured: true, healthy: false }), { label: "Error", tone: "error" });
  // null/undefined input is safe
  assert.equal(statusChip(null).tone, "neutral");
});

// SCRUM-81 shipped the payload's `status` field as 'configured' | 'ambient' |
// 'unconfigured'. This is the panel's third, distinct chip state: an
// unconfigured provider (no stored token) whose CLI (gh/git) is itself
// already authenticated.
test("statusChip renders a distinct third state for status: 'ambient'", () => {
  assert.deepEqual(
    statusChip({ configured: false, healthy: null, status: "ambient" }),
    { label: "Active via CLI auth", tone: "ambient" },
  );
});

test("statusChip renders all three states distinctly from a fixture payload list", () => {
  const fixture = [
    { name: "jira", configured: true, healthy: null, status: "configured" },
    { name: "github", configured: false, healthy: null, status: "ambient" },
    { name: "gitlab", configured: false, healthy: null, status: "unconfigured" },
  ];
  const chips = fixture.map(statusChip);
  assert.deepEqual(chips, [
    { label: "Configured", tone: "ok" },
    { label: "Active via CLI auth", tone: "ambient" },
    { label: "Unconfigured", tone: "neutral" },
  ]);
  // Every label and tone in the fixture is unique — no two states collapse.
  assert.equal(new Set(chips.map((c) => c.label)).size, 3);
  assert.equal(new Set(chips.map((c) => c.tone)).size, 3);
});

// A stored/configured integration is never relabelled "ambient" even if its
// CLI also happens to be authenticated — the backend guarantees this
// (list_integrations_with_ambient), and the panel must not second-guess it
// by checking `configured` before `status`.
test("statusChip never treats a configured integration as ambient", () => {
  assert.deepEqual(
    statusChip({ configured: true, healthy: true, status: "configured" }),
    { label: "Connected", tone: "ok" },
  );
});

test("KIND_LABEL covers every integration kind", () => {
  for (const k of ["issue_tracker", "vcs", "ci", "notifications"]) {
    assert.ok(KIND_LABEL[k], `missing kind label: ${k}`);
  }
});

test("BRAND_COLOR / NAME_LABEL / CONFIG_HINT cover all six integrations", () => {
  for (const name of ALL) {
    assert.match(BRAND_COLOR[name], /^#[0-9A-Fa-f]{6}$/, `brand color: ${name}`);
    assert.ok(NAME_LABEL[name], `name label: ${name}`);
    assert.ok(CONFIG_HINT[name], `config hint: ${name}`);
  }
  assert.equal(NAME_LABEL.circleci, "CircleCI");   // proper casing, not "Circleci"
});
