import { test } from "node:test";
import assert from "node:assert/strict";
import { capName, PROFILE_CAP, TOKENVAR_CAP } from "./capName.js";

test("a legit-length value passes through untouched", () => {
  assert.equal(capName("personal", PROFILE_CAP), "personal");
  // exactly at the cap — NOT truncated (the boundary must be inclusive)
  assert.equal(capName("a".repeat(32), 32), "a".repeat(32));
});

test("an over-long value is clipped to the cap and marked elided", () => {
  assert.equal(capName("a".repeat(33), 32), "a".repeat(32) + "…");
  // a credential pasted where a profile name is expected must NOT reach the DOM
  const token = "sk-ant-oat01-" + "x".repeat(120);
  const out = capName(token, PROFILE_CAP);
  assert.equal(out.length, 33); // 32 chars + one ellipsis
  assert.ok(out.endsWith("…"));
  assert.ok(!out.includes("x".repeat(90)), "the credential body must not be rendered");
});

test("the token-variable cap fits a max-length derived var but clips a longer one", () => {
  // CLAUDE_CODE_OAUTH_TOKEN_ (24) + a 32-char profile == 56 == the cap, kept whole
  const legit = "CLAUDE_CODE_OAUTH_TOKEN_" + "P".repeat(32);
  assert.equal(legit.length, TOKENVAR_CAP);
  assert.equal(capName(legit, TOKENVAR_CAP), legit);
  assert.equal(capName(legit + "Q", TOKENVAR_CAP), legit + "…");
});

test("non-strings pass through so null/undefined render as nothing", () => {
  assert.equal(capName(null, PROFILE_CAP), null);
  assert.equal(capName(undefined, PROFILE_CAP), undefined);
});
