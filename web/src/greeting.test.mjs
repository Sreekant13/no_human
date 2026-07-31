import test from "node:test";
import assert from "node:assert/strict";
import { greetingName, PLACEHOLDER_ADDRESSES } from "./greeting.js";

// The composer greets the operator. A WRONG name is worse than no name, so this
// only derives one when the source clearly looks like a human's address; anything
// else returns "" and the UI falls back to a plain "Hey there".
// Source: /api/config → notifications.email_to (verified live on the server).

test("derives a first name from a human-looking work address", () => {
  assert.equal(greetingName({ notifications: { email_to: "sam.rivera@example.com" } }), "Sam");
});

test("handles a single-part local address", () => {
  assert.equal(greetingName({ notifications: { email_to: "sam@example.com" } }), "Sam");
});

test("handles a hyphenated name", () => {
  assert.equal(greetingName({ notifications: { email_to: "anne-marie.dubois@x.com" } }), "Anne-Marie");
});

test("returns '' rather than guessing at a non-human address", () => {
  // Service accounts, bots, digits, and long slugs must never be greeted by name.
  const notHuman = [
    "svc-account@x.com",
    "ci-bot@x.com",
    "noreply@x.com",
    "user123@x.com",
    "a.b.c.d@x.com",
    "admin@x.com",
  ];
  for (const email of notHuman) {
    assert.equal(greetingName({ notifications: { email_to: email } }), "", email);
  }
});

test("returns '' and never throws when the config is missing or malformed", () => {
  assert.equal(greetingName(null), "");
  assert.equal(greetingName({}), "");
  assert.equal(greetingName({ notifications: {} }), "");
  assert.equal(greetingName({ notifications: { email_to: "" } }), "");
  assert.equal(greetingName({ notifications: { email_to: "not-an-email" } }), "");
  assert.equal(greetingName({ notifications: { email_to: 42 } }), "");
  assert.equal(greetingName({ integrations: {} }), "");
  assert.equal(greetingName({ integrations: { jira: {} } }), "");
  assert.equal(greetingName({ integrations: { jira: { email: 42 } } }), "");
});

test("the address config.py SHIPS is never treated as configured", () => {
  // notifications.email_to defaults to "dev@example.com" (config.py DEFAULTS),
  // so its presence proves nothing about the operator. Asserted through the
  // exported set so that changing the default cannot silently re-open the hole.
  assert.ok(PLACEHOLDER_ADDRESSES.has("dev@example.com"), "sanity: the shipped default is listed");
  for (const placeholder of PLACEHOLDER_ADDRESSES) {
    assert.equal(greetingName({ notifications: { email_to: placeholder } }), "", placeholder);
    assert.equal(greetingName({ notifications: { email_to: placeholder.toUpperCase() } }), "");
  }
  // And it must not shadow a real one behind it.
  assert.equal(
    greetingName({
      notifications: { email_to: "dev@example.com" },
      integrations: { jira: { email: "sam.rivera@example.com" } },
    }),
    "Sam",
  );
});

test("a name configured only on the Jira integration is still a configured name", () => {
  // An operator who set up Jira with their own account told us who they are.
  assert.equal(greetingName({ integrations: { jira: { email: "sam.rivera@example.com" } } }), "Sam");
  // The same shape rule applies — a Jira service account is not a person, and a
  // personal address carrying digits still yields no name worth greeting
  // ("Samrivera96" is exactly the wrong-name-is-worse-than-no-name case).
  assert.equal(greetingName({ integrations: { jira: { email: "ci-bot@example.com" } } }), "");
  assert.equal(greetingName({ integrations: { jira: { email: "samrivera96@example.com" } } }), "");
});

test("notifications.email_to still wins when both are real", () => {
  assert.equal(
    greetingName({
      notifications: { email_to: "anne-marie.dubois@example.com" },
      integrations: { jira: { email: "sam.rivera@example.com" } },
    }),
    "Anne-Marie",
  );
});

test("the agent's own git identity is never mistaken for the operator", () => {
  // git.agent_identity_email is the identity the PRODUCT commits under.
  assert.equal(
    greetingName({ git: { agent_identity_email: "no-human@users.noreply.github.com" } }),
    "",
  );
});
