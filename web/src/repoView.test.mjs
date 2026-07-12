import { test } from "node:test";
import assert from "node:assert/strict";
import { profileRows, profileStatus } from "./repoView.js";

test("profileRows shows only non-empty fields, in order", () => {
  const rows = profileRows({
    ecosystem: "python", test_cmd: "pytest -q", lint_cmd: "",
    ci: { enabled: true, backend: "gitlab" },
  });
  assert.deepEqual(rows.map(r => r.label), ["Ecosystem", "Unit tests", "Remote CI"]);
  assert.equal(rows[0].value, "python");
  assert.equal(rows[2].value, "gitlab");
});

test("profileRows on null/empty profile is an empty array, never throws", () => {
  assert.deepEqual(profileRows(null), []);
  assert.deepEqual(profileRows({}), []);
});

test("profileStatus reflects the trust ladder", () => {
  assert.equal(profileStatus(null), "not onboarded — map only");
  assert.equal(profileStatus({ confirmed: true, proven: { test_cmd: true } }), "onboarded & proven");
  assert.equal(profileStatus({ confirmed: false, proven: { test_cmd: true } }), "proven, unconfirmed");
  assert.equal(profileStatus({ confirmed: true, proven: {} }), "onboarded, unproven");
});
