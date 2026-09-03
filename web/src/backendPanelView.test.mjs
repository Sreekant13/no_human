import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import {
  backendPanelView,
  pendingBody,
  isSubmittable,
  applyError,
  localFields,
  showLocalFields,
  submitBody,
  canSubmit,
  shortReason,
} from "./backendPanelView.js";

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

// ── local backend config fields ──────────────────────────────────────────
//
// The row can set the local backend's two config values (llm.local_model /
// llm.local_base_url) inline. Everything — the backend id, each field's config
// key, label, placeholder and current value — is read out of the payload's
// `local_fields`, mirroring how the option list flows from the payload; the
// fixture below is the exact shape backend_settings.backend_payload sends.
function withLocalFields(overrides = {}, localOverrides = {}) {
  return payload({
    local_fields: {
      backend: "local",
      fields: [
        { key: "local_model", value: "", label: "Local model", placeholder: "the model id" },
        { key: "local_base_url", value: "", label: "Local base URL", placeholder: "http://localhost:8000" },
      ],
      ...localOverrides,
    },
    ...overrides,
  });
}

test("localFields is null for a payload without local_fields (older server)", () => {
  assert.equal(localFields(payload()), null);
  assert.equal(localFields(null), null);
  assert.equal(localFields({ local_fields: { backend: "local" } }), null, "no fields array");
});

test("localFields copies backend id, key, label, placeholder and value from the payload", () => {
  const lf = localFields(withLocalFields({}, {
    fields: [{ key: "local_model", value: "m1", label: "Local model", placeholder: "hint" }],
  }));
  assert.equal(lf.backend, "local");
  assert.deepEqual(lf.fields, [{ key: "local_model", value: "m1", label: "Local model", placeholder: "hint" }]);
});

test("showLocalFields is true only when the selected backend is the local one", () => {
  const p = withLocalFields();
  assert.equal(showLocalFields(p, null), false, "current is claude");
  assert.equal(showLocalFields(p, "codex"), false);
  assert.equal(showLocalFields(p, "local"), true, "pending local reveals the fields");
  assert.equal(showLocalFields(withLocalFields({ current: "local" }), null), true, "already on local");
  assert.equal(showLocalFields(payload(), "local"), false, "no local_fields -> never shown");
});

test("submitBody carries only changed local fields, and only when local is selected", () => {
  const p = withLocalFields();
  // Not local: local field values are ignored entirely.
  assert.deepEqual(submitBody(p, "codex", { local_model: "x", local_base_url: "y" }), { backend: "codex" });
  // Switching to local with both fields filled: backend + both fields.
  assert.deepEqual(
    submitBody(p, "local", { local_model: "m", local_base_url: "http://localhost:8000" }),
    { backend: "local", local_model: "m", local_base_url: "http://localhost:8000" },
  );
  // Editing one field while already on local (no backend change): only that field.
  const onLocal = withLocalFields({ current: "local" }, {
    fields: [
      { key: "local_model", value: "old", label: "Local model", placeholder: "" },
      { key: "local_base_url", value: "http://localhost:8000", label: "Local base URL", placeholder: "" },
    ],
  });
  assert.deepEqual(
    submitBody(onLocal, null, { local_model: "new", local_base_url: "http://localhost:8000" }),
    { local_model: "new" },
  );
  // Nothing changed -> null (Save disabled), even with the fields prefilled.
  assert.equal(submitBody(onLocal, null, { local_model: "old", local_base_url: "http://localhost:8000" }), null);
  // A blank edit that differs from the server value still forms a body (the
  // gate below is what refuses it, not submitBody).
  assert.deepEqual(submitBody(onLocal, null, { local_model: "", local_base_url: "http://localhost:8000" }), { local_model: "" });
});

test("canSubmit requires every local field non-blank when local is selected, and bypasses the availability gate for it", () => {
  const p = withLocalFields(); // 'local' is unavailable in the base fixture
  assert.equal(isSubmittable(p, "local"), false, "precondition: local greyed out for availability");
  // Blank fields: refused.
  assert.equal(canSubmit(p, "local", { local_model: "", local_base_url: "" }), false);
  assert.equal(canSubmit(p, "local", { local_model: "m", local_base_url: "" }), false, "one blank still refused");
  // Both filled: allowed DESPITE local being unavailable — configuring it now.
  assert.equal(canSubmit(p, "local", { local_model: "m", local_base_url: "http://localhost:8000" }), true);
  // Non-local pending keeps the existing availability gate untouched.
  assert.equal(canSubmit(p, "codex", {}), true);
  assert.equal(
    canSubmit(payload({ options: [
      { id: "claude", available: true, reason: "" },
      { id: "codex", available: false, reason: "not logged in" },
      { id: "local", available: false, reason: "x" },
    ] }), "codex", {}),
    false,
    "an unavailable non-local backend stays blocked",
  );
});

test("backendPanelView surfaces localFields (null when the payload omits them)", () => {
  assert.equal(backendPanelView(payload()).localFields, null);
  assert.equal(backendPanelView(withLocalFields()).localFields.backend, "local");
});

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

// ── shortReason ─────────────────────────────────────────────────────────

const CODEX_UNAVAILABLE_REASON =
  "— the coder backend is 'codex' (worker.backend, or a task's --backend) but no OPENAI_API_KEY was found, and llm.codex_auth_mode is 'api_key' (the default). The Codex backend runs on YOUR OWN OpenAI API key in this mode.\nAdd the key to ~/.no_human/.env (chmod 600):\n  echo 'OPENAI_API_KEY=sk-...' >> ~/.no_human/.env";

test("shortReason: first sentence only, leading dash stripped, one line, capped at 140 chars", () => {
  const s = shortReason(CODEX_UNAVAILABLE_REASON);
  assert.ok(!s.includes("\n"), s);
  assert.ok(s.startsWith("the coder backend is 'codex'"), s);
  assert.ok(s.length <= 140, String(s.length));
  assert.equal(shortReason(LOCAL_UNAVAILABLE_REASON), LOCAL_UNAVAILABLE_REASON);
  assert.equal(shortReason("First. Second."), "First.");
  assert.equal(shortReason(""), "");
  assert.equal(shortReason(undefined), "");
});

test("options carry a one-line short reason; available options carry an empty one", () => {
  const view = backendPanelView(payload({
    options: [
      { id: "claude", available: true, reason: "" },
      { id: "codex", available: false, reason: CODEX_UNAVAILABLE_REASON },
      { id: "local", available: false, reason: LOCAL_UNAVAILABLE_REASON },
    ],
  }));
  const byId = Object.fromEntries(view.options.map((o) => [o.id, o]));
  assert.equal(byId.claude.short, "");
  assert.equal(byId.codex.reason, CODEX_UNAVAILABLE_REASON); // full text still there for the tooltip
  assert.equal(byId.codex.short, shortReason(CODEX_UNAVAILABLE_REASON));
  assert.equal(byId.local.short, LOCAL_UNAVAILABLE_REASON);
});
