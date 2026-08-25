import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { modelsPanelView, pendingBody, resetBody, applyError } from "./modelsPanelView.js";

// One fixture payload shared by every test below — the shape GET /api/models
// actually returns (see core/model_settings.py::models_payload). Nothing in
// this file invents an id, price, default or rule string of its own; every
// expectation is read out of this fixture.
const VENDOR_PIN_NOTE =
  "only Claude ids may run this role — the coder's backend (Claude/Codex/local) is a separate control.";
const DISABLED_REASON =
  "'gpt-5-codex' cannot be set as llm.primary_model: only the Claude backend reads that key.";
const COST_NOTE =
  "The operator's 2026-08-11 A/B reverted this role from claude-opus-5 back to claude-opus-4-8.";

function payload(overrides = {}) {
  return {
    roles: [
      {
        role: "coder",
        key: "primary_model",
        current: "claude-sonnet-5",
        default: "claude-sonnet-5",
        note: "",
        cost_note: "",
        options: [
          {
            id: "claude-sonnet-5",
            price_class: { label: "medium", input_rate: 3, output_rate: 15 },
            is_default: true,
            note: "",
            requires_backend: false,
          },
          {
            id: "gpt-5-codex",
            price_class: { label: "medium", input_rate: 3, output_rate: 15 },
            is_default: false,
            note: "",
            requires_backend: true,
            disabled_reason: DISABLED_REASON,
          },
        ],
      },
      {
        role: "reviewer",
        key: "review_model",
        current: "claude-opus-4-8",
        default: "claude-opus-4-8",
        note: VENDOR_PIN_NOTE,
        cost_note: COST_NOTE,
        options: [
          {
            id: "claude-opus-4-8",
            price_class: { label: "high", input_rate: 15, output_rate: 75 },
            is_default: true,
            note: VENDOR_PIN_NOTE,
            requires_backend: false,
          },
          {
            id: "claude-opus-5",
            price_class: { label: "high", input_rate: 18, output_rate: 90 },
            is_default: false,
            note: VENDOR_PIN_NOTE,
            requires_backend: false,
          },
        ],
      },
      {
        role: "planner",
        key: "planner_model",
        current: "claude-opus-5",
        default: "claude-opus-5",
        note: VENDOR_PIN_NOTE,
        cost_note: "",
        options: [
          {
            id: "claude-opus-5",
            price_class: { label: "high", input_rate: 18, output_rate: 90 },
            is_default: true,
            note: VENDOR_PIN_NOTE,
            requires_backend: false,
          },
        ],
      },
      {
        role: "supervisor",
        key: "supervisor_model",
        current: "claude-sonnet-5",
        default: "claude-sonnet-5",
        note: VENDOR_PIN_NOTE,
        cost_note: "",
        options: [
          {
            id: "claude-sonnet-5",
            price_class: { label: "medium", input_rate: 3, output_rate: 15 },
            is_default: true,
            note: VENDOR_PIN_NOTE,
            requires_backend: false,
          },
        ],
      },
      {
        role: "utility",
        key: "utility_model",
        current: "claude-haiku-4-5",
        default: "claude-haiku-4-5",
        note: VENDOR_PIN_NOTE,
        cost_note: "",
        options: [
          {
            id: "claude-haiku-4-5",
            price_class: { label: "low", input_rate: 0.8, output_rate: 4 },
            is_default: true,
            note: VENDOR_PIN_NOTE,
            requires_backend: false,
          },
        ],
      },
    ],
    restart_required: false,
    ...overrides,
  };
}

// 1. Five rows in payload order, each field copied verbatim from the
// payload — plus a source-text guard that neither view-model file spells out
// a model id or a config key of its own. This is the "no literals in JS" AC
// made executable, not just asserted in prose.
test("modelsPanelView returns five rows in payload order, fields copied from the payload", () => {
  const p = payload();
  const view = modelsPanelView(p);
  assert.equal(view.unavailable, false);
  assert.equal(view.rows.length, 5);
  assert.deepEqual(
    view.rows.map((r) => r.role),
    ["coder", "reviewer", "planner", "supervisor", "utility"],
  );
  view.rows.forEach((row, i) => {
    const src = p.roles[i];
    assert.equal(row.role, src.role);
    assert.equal(row.key, src.key);
    assert.equal(row.current, src.current);
    assert.equal(row.default, src.default);
    assert.deepEqual(
      row.options.map((o) => o.id),
      src.options.map((o) => o.id),
    );
  });
});

test("neither view-model file hardcodes a model id, vendor prefix, or config key", () => {
  const files = {
    "modelsPanelView.js": readFileSync(new URL("./modelsPanelView.js", import.meta.url), "utf8"),
    "ModelsPanel.jsx": readFileSync(new URL("./ModelsPanel.jsx", import.meta.url), "utf8"),
  };
  for (const [name, src] of Object.entries(files)) {
    assert.doesNotMatch(src, /claude-/, `${name} names a claude- id`);
    assert.doesNotMatch(src, /gpt-/, `${name} names a gpt- id`);
    assert.doesNotMatch(src, /primary_model|review_model|planner_model|supervisor_model|utility_model/,
      `${name} spells out a config key`);
  }
});

// 2. requires_backend flips disabled + reason; false -> enabled, empty reason.
test("options carry disabled/reason straight from requires_backend and disabled_reason", () => {
  const view = modelsPanelView(payload());
  const coder = view.rows.find((r) => r.role === "coder");
  const claudeOpt = coder.options.find((o) => o.id === "claude-sonnet-5");
  const gptOpt = coder.options.find((o) => o.id === "gpt-5-codex");
  assert.equal(claudeOpt.disabled, false);
  assert.equal(claudeOpt.reason, "");
  assert.equal(gptOpt.disabled, true);
  assert.equal(gptOpt.reason, DISABLED_REASON);
});

// 3. Pinned-role rows surface VENDOR_PIN_NOTE verbatim as row.note; coder "".
test("pinned-role rows carry the server's note verbatim; the coder row does not", () => {
  const view = modelsPanelView(payload());
  const byRole = Object.fromEntries(view.rows.map((r) => [r.role, r]));
  assert.equal(byRole.coder.note, "");
  for (const role of ["reviewer", "planner", "supervisor", "utility"]) {
    assert.equal(byRole[role].note, VENDOR_PIN_NOTE);
  }
});

// 4. pendingBody returns only edited keys; a selection equal to current is
// absent even if present in `pending`.
test("pendingBody sends only keys whose pending value differs from current", () => {
  const p = payload();
  const pending = {
    review_model: "claude-opus-5", // changed
    primary_model: "claude-sonnet-5", // same as current -> must be dropped
  };
  assert.deepEqual(pendingBody(p, pending), { review_model: "claude-opus-5" });
});

test("pendingBody is empty for an empty or missing pending map", () => {
  const p = payload();
  assert.deepEqual(pendingBody(p, {}), {});
  assert.deepEqual(pendingBody(p, undefined), {});
  assert.deepEqual(pendingBody(null, { review_model: "x" }), {});
});

// 5. resetBody equals the fixture's default values, read from the fixture.
test("resetBody equals the payload's own defaults for every role that has drifted", () => {
  const p = payload();
  p.roles[1].current = "claude-opus-5"; // reviewer drifted from its default
  const expected = {};
  for (const r of p.roles) {
    if (r.default !== r.current) expected[r.key] = r.default;
  }
  assert.deepEqual(resetBody(p), expected);
  assert.deepEqual(resetBody(p), { review_model: p.roles[1].default });
});

test("resetBody is empty when every role already sits at its default", () => {
  assert.deepEqual(resetBody(payload()), {});
});

// 6. applyError reverts everything and surfaces the server text verbatim.
test("applyError clears all pending edits and returns the server detail verbatim", () => {
  const detail = "'gpt-5.4' has no published price; refusing to run an unpriced model.";
  const result = applyError({ review_model: "gpt-5.4", planner_model: "claude-opus-5" }, detail);
  assert.deepEqual(result.pending, {});
  assert.equal(result.error, detail);
});

// 7. showRestartBanner follows payload.restart_required both directions, and
// is never inferred from current !== default.
test("showRestartBanner follows restart_required, not any current/default mismatch", () => {
  const p1 = payload({ restart_required: true });
  assert.equal(modelsPanelView(p1).showRestartBanner, true);

  const p2 = payload({ restart_required: false });
  assert.equal(modelsPanelView(p2).showRestartBanner, false);

  // Drift present, restart_required false -> still no banner.
  const p3 = payload({ restart_required: false });
  p3.roles[1].current = "claude-opus-5";
  assert.equal(modelsPanelView(p3).showRestartBanner, false);
});

// 8. Reviewer row costNote equals roles[i].cost_note; absent -> ""; no other
// row carries one.
test("only the reviewer row carries a costNote, copied verbatim from cost_note", () => {
  const view = modelsPanelView(payload());
  const byRole = Object.fromEntries(view.rows.map((r) => [r.role, r]));
  assert.equal(byRole.reviewer.costNote, COST_NOTE);
  for (const role of ["coder", "planner", "supervisor", "utility"]) {
    assert.equal(byRole[role].costNote, "");
  }
});

test("costNote is empty when the payload omits cost_note entirely", () => {
  const p = payload();
  delete p.roles[1].cost_note;
  const view = modelsPanelView(p);
  assert.equal(view.rows.find((r) => r.role === "reviewer").costNote, "");
});

// 9. modelsPanelView(null) / {} -> {rows: [], unavailable: true}.
test("a missing or empty payload is reported unavailable with no rows", () => {
  assert.deepEqual(modelsPanelView(null), { unavailable: true, showRestartBanner: false, rows: [] });
  assert.deepEqual(modelsPanelView({}), { unavailable: true, showRestartBanner: false, rows: [] });
  assert.deepEqual(modelsPanelView({ roles: [] }), { unavailable: true, showRestartBanner: false, rows: [] });
});
