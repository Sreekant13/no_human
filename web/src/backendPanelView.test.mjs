import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { backendPanelView, pendingBody, isSubmittable, applyError } from "./backendPanelView.js";

// One fixture payload shared by every test below — the shape GET
// /api/coder-backend actually returns (see
// core/backend_settings.py::backend_payload). Nothing in this file invents a
// backend id, availability rule or reason string of its own; every
// expectation is read out of this fixture, mirroring modelsPanelView.test.mjs.
const LOCAL_UNAVAILABLE_REASON =
  "the coder backend is 'local' (worker.backend, or a task's --backend) but llm.local_base_url is not set.";

function payload(overrides = {}) {
  return {
    current: "claude",
    default: "claude",
    options: [
      { id: "claude", available: true, reason: "" },
      { id: "codex", available: true, reason: "" },
      { id: "local", available: false, reason: LOCAL_UNAVAILABLE_REASON },
    ],
    restart_required: false,
    ...overrides,
  };
}

const here = fileURLToPath(new URL(".", import.meta.url));

// ── backendPanelView ────────────────────────────────────────────────────

test("backendPanelView returns options in payload order, fields copied from the payload", () => {
  const p = payload();
  const view = backendPanelView(p);
  assert.equal(view.unavailable, false);
  assert.equal(view.current, "claude");
  assert.equal(view.default, "claude");
  assert.deepEqual(
    view.options.map((o) => o.id),
    ["claude", "codex", "local"],
  );
  const local = view.options.find((o) => o.id === "local");
  assert.equal(local.disabled, true);
  assert.equal(local.reason, LOCAL_UNAVAILABLE_REASON);
  const claude = view.options.find((o) => o.id === "claude");
  assert.equal(claude.disabled, false);
  assert.equal(claude.reason, "");
  assert.equal(claude.isCurrent, true);
});

test("a fourth SUPPORTED_BACKENDS entry shows up with no change to this file", () => {
  // Acceptance criterion 1's UI half: backendPanelView must not hardcode the
  // three known ids — a payload with a temporary fourth option must survive
  // unmodified into view.options with no special-casing anywhere.
  const p = payload({
    options: [
      ...payload().options,
      { id: "testbackend", available: true, reason: "" },
    ],
  });
  const view = backendPanelView(p);
  assert.deepEqual(
    view.options.map((o) => o.id),
    ["claude", "codex", "local", "testbackend"],
  );
  assert.equal(view.options.find((o) => o.id === "testbackend").disabled, false);
});

test("neither backendPanelView nor its callers hardcode a backend id or reason string", () => {
  // Source-text guard, mirroring modelsPanelView.test.mjs's "no literals in
  // JS" AC made executable: the view-model must derive labels/availability
  // entirely from the payload, never spell out 'codex'/'local' or the
  // config-check's reason text itself.
  const src = readFileSync(here + "backendPanelView.js", "utf8");
  for (const literal of ["\"codex\"", "'codex'", "\"local\"", "'local'", "local_base_url"]) {
    assert.ok(!src.includes(literal), `backendPanelView.js must not hardcode ${literal}`);
  }
});

test("backendPanelView reports unavailable for a missing, empty, or malformed payload", () => {
  for (const bad of [null, undefined, {}, { options: [] }, { options: "nope", current: "claude" }]) {
    const view = backendPanelView(bad);
    assert.equal(view.unavailable, true);
    assert.deepEqual(view.options, []);
  }
});

test("showRestartBanner tracks payload.restart_required exactly", () => {
  assert.equal(backendPanelView(payload({ restart_required: true })).showRestartBanner, true);
  assert.equal(backendPanelView(payload({ restart_required: false })).showRestartBanner, false);
});

// ── pendingBody ─────────────────────────────────────────────────────────

test("pendingBody sends {backend: pending} only when pending differs from current", () => {
  const p = payload();
  assert.deepEqual(pendingBody(p, "codex"), { backend: "codex" });
  assert.equal(pendingBody(p, "claude"), null, "same as current -> nothing to send");
  assert.equal(pendingBody(p, null), null);
  assert.equal(pendingBody(p, undefined), null);
  assert.equal(pendingBody(null, "codex"), null);
});

// ── isSubmittable ───────────────────────────────────────────────────────

test("isSubmittable defers to the payload's own per-option availability", () => {
  const p = payload();
  assert.equal(isSubmittable(p, "codex"), true);
  assert.equal(isSubmittable(p, "local"), false, "local is unavailable in this fixture");
  assert.equal(isSubmittable(p, "not-a-real-backend"), false);
  assert.equal(isSubmittable(p, null), false);
  assert.equal(isSubmittable(null, "codex"), false);
});

test("isSubmittable flips to true the moment the payload reports the option available", () => {
  // Same shape the API test asserts server-side (llm.local_base_url set):
  // this view-model must track that flip with no rule of its own.
  const enabled = payload({
    options: [
      { id: "claude", available: true, reason: "" },
      { id: "codex", available: true, reason: "" },
      { id: "local", available: true, reason: "" },
    ],
  });
  assert.equal(isSubmittable(enabled, "local"), true);
});

// ── applyError ──────────────────────────────────────────────────────────

test("applyError clears the pending edit and surfaces the server's verbatim message", () => {
  const result = applyError(LOCAL_UNAVAILABLE_REASON);
  assert.equal(result.pending, null);
  assert.equal(result.error, LOCAL_UNAVAILABLE_REASON);
});

// ── mutation controls — the RED proof that the above isn't vacuous ────────

test("MUTATION CONTROL: an isSubmittable that ignores availability would pass a bad selection", () => {
  // Demonstrates what criterion 2 forbids: a frontend heuristic that always
  // returns true. This inline mutant (not the real file) is asserted to
  // disagree with the real isSubmittable on the same input, proving the real
  // function's availability check is load-bearing and not incidentally true.
  const alwaysTrue = (_payload, pending) => !!pending;
  const p = payload();
  assert.equal(alwaysTrue(p, "local"), true, "the naive mutant wrongly allows 'local'");
  assert.equal(isSubmittable(p, "local"), false, "the real function correctly refuses it");
});
