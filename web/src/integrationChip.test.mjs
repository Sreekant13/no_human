import test from "node:test";
import assert from "node:assert/strict";
import { statusChip, KIND_LABEL, BRAND_COLOR, NAME_LABEL, CONFIG_HINT } from "./integrationChip.js";

const ALL = ["jira", "github", "gitlab", "jenkins", "circleci", "slack"];

test("statusChip maps the four states", () => {
  assert.deepEqual(statusChip({ configured: false }), { label: "Unconfigured", tone: "neutral" });
  assert.deepEqual(statusChip({ configured: true, healthy: null }), { label: "Configured", tone: "ok" });
  assert.deepEqual(statusChip({ configured: true, healthy: true }), { label: "Connected", tone: "ok" });
  assert.deepEqual(statusChip({ configured: true, healthy: false }), { label: "Error", tone: "error" });
  // null/undefined input is safe
  assert.equal(statusChip(null).tone, "neutral");
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
