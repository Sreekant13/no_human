import test from "node:test";
import assert from "node:assert/strict";
import { greetingName } from "./greeting.js";

// The composer greets the operator. A WRONG name is worse than no name, so this
// only derives one when the source clearly looks like a human's address; anything
// else returns "" and the UI falls back to a plain "Hey there".
// Source: /api/config → notifications.email_to (verified live on the server).

test("derives a first name from a human-looking work address", () => {
  assert.equal(greetingName({ notifications: { email_to: "dev@example.com" } }), "Eyal");
});

test("handles a single-part local address", () => {
  assert.equal(greetingName({ notifications: { email_to: "eyal@example.com" } }), "Eyal");
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
});
