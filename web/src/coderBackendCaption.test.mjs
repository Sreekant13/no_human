import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { coderBackendCaption, effectiveCoderBackend } from "./coderBackendCaption.js";

// Operator feedback 2026-09-01: the pinned-roles disclosure was rendered
// unconditionally, so every user starting a task on the config default saw
// backend-internals jargon that only matters once the choice actually
// differs from worker.backend. These pin the fix: no caption on the config
// default ("" — TaskComposer.jsx's untouched-selector value), one plain
// sentence once a non-default backend is picked, and the disclosed roles
// stay server-derived, never a second hardcoded list in this file.

const ROLES = ["reviewer", "planner", "supervisor", "utility"];

const here = fileURLToPath(new URL(".", import.meta.url));
const helperSource = readFileSync(here + "coderBackendCaption.js", "utf8");
const composerJsx = readFileSync(here + "TaskComposer.jsx", "utf8");

// ── AC 1 — default: no caption; non-default: one plain sentence ────────────

test("the config-default selection renders no pinned-roles caption", () => {
  assert.equal(coderBackendCaption("", ROLES), "");
  assert.equal(coderBackendCaption(undefined, ROLES), "");
  assert.equal(coderBackendCaption(null, ROLES), "");
  assert.equal(coderBackendCaption("   ", ROLES), "");
});

test("a non-default backend renders the disclosure", () => {
  for (const id of ["codex", "local", "claude"]) {
    const out = coderBackendCaption(id, ROLES);
    assert.notEqual(out, "");
    assert.match(out, new RegExp(id));
  }
});

test("the caption is one plain sentence", () => {
  const out = coderBackendCaption("codex", ROLES);
  const terminalPeriods = out.match(/\./g) || [];
  assert.equal(terminalPeriods.length, 1, "exactly one terminal period");
  assert.ok(out.endsWith("."), "ends with the terminal period");
  assert.doesNotMatch(out, /;/);
  assert.ok(out.length < 90, `expected under ~90 chars, got ${out.length}`);
  assert.doesNotMatch(out, /Coder backend only/);
});

// ── AC 2 — meaning preserved when shown ─────────────────────────────────────

test("the shown caption still says the other roles stay on Claude", () => {
  const out = coderBackendCaption("codex", ROLES);
  for (const role of ROLES) {
    assert.match(out, new RegExp(role));
  }
  assert.match(out, /Claude/);
});

test("role names are server-derived, never a literal in this file", () => {
  const invented = ["triage-bot", "archivist"];
  const out = coderBackendCaption("local", invented);
  for (const role of invented) {
    assert.match(out, new RegExp(role));
  }
  assert.doesNotMatch(helperSource, /reviewer|planner|supervisor/);
});

test("an older server that sends no pinned roles still discloses the coder-only scope", () => {
  const empty = coderBackendCaption("codex", []);
  assert.notEqual(empty, "");
  assert.doesNotMatch(empty, /reviewer|planner|supervisor/);

  const missing = coderBackendCaption("codex", undefined);
  assert.notEqual(missing, "");
  assert.doesNotMatch(missing, /reviewer|planner|supervisor/);
});

// ── effectiveCoderBackend — gate the disclosure on the EFFECTIVE backend ───
// Independent review 2026-09-02: the checkpoint gated the caption on the
// picker's own value only, so an install configured with worker.backend:
// codex and the picker left untouched showed NO disclosure at all — exactly
// the install the d35aa60e constraint (only the coder role ever moves off
// Claude) was written to surface. effectiveCoderBackend fixes that: an
// explicit pick always wins; otherwise the server's resolved
// coder_backend_effective counts only when it differs from the server's own
// coder_backend_default. Both values are server-derived (GET /api/config) —
// this file has no literal backend name of its own.

test("a configured non-default backend discloses without any pick", () => {
  const config = { coder_backend_effective: "codex", coder_backend_default: "claude" };
  assert.equal(effectiveCoderBackend("", config), "codex");
  const out = coderBackendCaption(effectiveCoderBackend("", config), ROLES);
  assert.match(out, /codex/);
  for (const role of ROLES) {
    assert.match(out, new RegExp(role));
  }
});

test("a plain default install discloses nothing", () => {
  const configs = [
    { coder_backend_effective: "claude", coder_backend_default: "claude" },
    {},
    { coder_backend_effective: "claude" },
    null,
  ];
  for (const config of configs) {
    assert.equal(effectiveCoderBackend("", config), "");
    assert.equal(coderBackendCaption(effectiveCoderBackend("", config), ROLES), "");
  }
});

test("an explicit pick always wins over config", () => {
  const config = { coder_backend_effective: "codex", coder_backend_default: "claude" };
  assert.equal(effectiveCoderBackend("local", config), "local");
  // Even picking the SAME name the server already resolved to is still an
  // explicit disclosure-worthy choice, not a no-op.
  assert.equal(effectiveCoderBackend("claude", config), "claude");
});

test("a server that sends no default stays silent on the effective value", () => {
  // An older server (pre-this-change) sends coder_backend_effective with no
  // coder_backend_default — degrade to silence, never a guessed disclosure.
  assert.equal(effectiveCoderBackend("", { coder_backend_effective: "codex" }), "");
});

test("effectiveCoderBackend names no backend of its own (ablation control)", () => {
  const helperSrc = readFileSync(here + "coderBackendCaption.js", "utf8");
  assert.doesNotMatch(helperSrc, /["'`]claude["'`]/i);
  assert.doesNotMatch(helperSrc, /reviewer|planner|supervisor/);
  // Invented names must flow through untouched — proof the "" result is a
  // genuine equality check against the server's own default, not a
  // hardcoded "claude" comparison in disguise.
  assert.equal(
    effectiveCoderBackend("", { coder_backend_effective: "zeta", coder_backend_default: "omega" }),
    "zeta",
  );
  assert.equal(
    effectiveCoderBackend("", { coder_backend_effective: "zeta", coder_backend_default: "zeta" }),
    "",
  );
});

// ── Source-binding — the composer must actually use the helper ─────────────

test("the composer imports and calls coderBackendCaption", () => {
  assert.match(
    composerJsx,
    /import\s*\{\s*coderBackendCaption\s*,\s*effectiveCoderBackend\s*\}\s*from\s*["']\.\/coderBackendCaption\.js["']/,
  );
  assert.match(composerJsx, /coderBackendCaption\(\s*effectiveBackend\s*,\s*claudePinnedRoles\s*\)/);
});

test("the caption paragraph is gated on the helper's return, not rendered unconditionally", () => {
  // The old defect: `{backendOptions.length > 0 && (<p>Coder backend only…`
  // rendered on every load. The fix must gate on the helper's (possibly
  // empty) return value as well, so the <p> disappears on the config default.
  assert.doesNotMatch(composerJsx, /backendOptions\.length\s*>\s*0\s*&&\s*\(\s*<p/);
  assert.match(composerJsx, /backendCaption\s*&&/);
});

// ── Composer wiring — effective backend feeds the caption, config feeds it ──

test("the composer resolves the effective backend from config before captioning it", () => {
  // The picker's raw "" must never be handed straight to coderBackendCaption
  // again — it has to pass through effectiveCoderBackend first, reading the
  // whole server-derived `config` object (never a JS-side
  // `config?.worker?.backend` re-derivation of the same precedence
  // `resolve_backend_name` already owns server-side).
  assert.match(composerJsx, /effectiveCoderBackend\(\s*backend\s*,\s*config\s*\)/);
  assert.doesNotMatch(composerJsx, /config\?\.\s*worker\s*\?\.\s*backend/);
  const effectiveIdx = composerJsx.search(/effectiveCoderBackend\(\s*backend\s*,\s*config\s*\)/);
  const captionIdx = composerJsx.search(/coderBackendCaption\(\s*effectiveBackend\s*,\s*claudePinnedRoles\s*\)/);
  assert.ok(effectiveIdx > -1 && captionIdx > -1, "both call sites must exist");
  assert.ok(effectiveIdx < captionIdx, "effectiveCoderBackend must run before coderBackendCaption uses its result");
});
