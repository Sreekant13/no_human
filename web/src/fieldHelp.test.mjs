import test from "node:test";
import assert from "node:assert/strict";
import { hintId, hostLabel } from "./fieldHelp.js";

test("hintId is a stable per-field DOM id", () => {
  assert.equal(hintId("linear", "team_key"), "hint-linear-team_key");
  assert.equal(hintId("jira", "api_token"), "hint-jira-api_token");
});

test("hostLabel is the bare host of the help URL", () => {
  assert.equal(hostLabel("https://linear.app/settings/teams"), "linear.app");
  assert.equal(hostLabel("https://id.atlassian.com/manage-profile/security/api-tokens"),
               "id.atlassian.com");
  // A non-URL never throws — the "Open ↗" link just gets no host label.
  assert.equal(hostLabel(""), "");
  assert.equal(hostLabel("not a url"), "");
});
