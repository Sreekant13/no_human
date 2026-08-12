import test from "node:test";
import assert from "node:assert/strict";
import { testResultView } from "./integrationTestResult.js";

// Reproduces the walk-found bug: an unconfigured-but-ambiently-authenticated
// GitHub returns healthy: null, status: "ambient" — Integrations.jsx used to
// gate rendering on healthy === true/false, so this payload rendered nothing.
test("ambient CLI-auth payload renders success, not silence", () => {
  const status = {
    name: "github", configured: false, healthy: null,
    status: "ambient", detail: "available via ambient CLI auth",
  };
  assert.deepEqual(testResultView(status), {
    tone: "ok", icon: "✓", text: "available via ambient CLI auth",
  });
});

test("token/configured success renders the authenticated identity", () => {
  const jira = testResultView({ healthy: true, detail: "authenticated as eyal golan" });
  assert.equal(jira.tone, "ok");
  assert.equal(jira.text, "authenticated as eyal golan");

  const githubConfigured = testResultView({
    healthy: true, status: "configured",
    detail: "github_actions · repo — verified by the backend at run time",
  });
  assert.equal(githubConfigured.tone, "ok");
  assert.equal(githubConfigured.text, "github_actions · repo — verified by the backend at run time");

  // The ambient and token-configured success variants must read differently.
  const ambient = testResultView({
    healthy: null, status: "ambient", detail: "available via ambient CLI auth",
  });
  assert.notEqual(ambient.text, githubConfigured.text);
});

test("success with no detail falls back to an identity field, then to 'connected'", () => {
  assert.equal(testResultView({ healthy: true, username: "octocat" }).text,
    "authenticated as octocat");
  assert.equal(testResultView({ healthy: true }).text, "connected");
});

test("failure payload renders the reason", () => {
  const notConfigured = testResultView({ healthy: false, detail: "not configured" });
  assert.equal(notConfigured.tone, "err");
  assert.equal(notConfigured.icon, "✕");
  assert.equal(notConfigured.text, "not configured");

  const http401 = testResultView({ healthy: false, detail: "HTTP 401" });
  assert.equal(http401.tone, "err");
  assert.equal(http401.text, "HTTP 401");
});

test("a thrown error (non-200 / transport) renders the error message", () => {
  const result = testResultView(null, new Error("HTTP 500"));
  assert.equal(result.tone, "err");
  assert.match(result.text, /HTTP 500/);
});

test("no payload / unknown shape fails closed to a failure, never silence", () => {
  for (const shape of [null, undefined, {}, { healthy: null }, "nope"]) {
    const result = testResultView(shape);
    assert.equal(result.tone, "err", `shape ${JSON.stringify(shape)} must be tone err`);
    assert.ok(result.text.length > 0, `shape ${JSON.stringify(shape)} must have non-empty text`);
  }
});

test("every branch returns exactly one result", () => {
  const shapes = [
    { name: "jira", healthy: true, detail: "authenticated as eyal golan" },
    { name: "linear", healthy: false, detail: "not configured" },
    { name: "github", healthy: null, status: "ambient", detail: "available via ambient CLI auth" },
    { name: "github", healthy: true, status: "configured", detail: "verified by the backend at run time" },
    { name: "gitlab", healthy: null, status: "unconfigured" },
    null, undefined, {}, "nope",
  ];
  for (const shape of shapes) {
    const result = testResultView(shape);
    assert.ok(["ok", "err"].includes(result.tone));
    assert.ok(["✓", "✕"].includes(result.icon));
    assert.ok(result.text.length > 0);
  }
  // Thrown-error path too.
  const errResult = testResultView(null, new Error("boom"));
  assert.ok(["ok", "err"].includes(errResult.tone));
  assert.ok(["✓", "✕"].includes(errResult.icon));
  assert.ok(errResult.text.length > 0);
});
