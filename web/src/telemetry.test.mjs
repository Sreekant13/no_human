import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { initTelemetry, telemetryConsent, captureScreen, _resetForTests } from "./telemetry.js";

const SRC = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(join(SRC, p), "utf8");

function fakePosthogModule() {
  const calls = { init: [], register: [], capture: [] };
  return {
    calls,
    module: {
      default: {
        init: (...a) => calls.init.push(a),
        register: (...a) => calls.register.push(a),
        capture: (...a) => calls.capture.push(a),
      },
    },
  };
}

// ── consent gate: no consent means posthog-js is NEVER imported ──────────────

test("no consent → importer never called, init returns null", async () => {
  _resetForTests();
  const imported = [];
  const importer = async (m) => { imported.push(m); return fakePosthogModule().module; };
  for (const cfg of [
    undefined,
    null,
    {},
    { telemetry: null },
    { telemetry: { enabled: false, posthog_publishable: "phc_x" } },
    { telemetry: { enabled: true } }, // consented but no client token
  ]) {
    assert.equal(await initTelemetry(cfg, { importer }), null);
  }
  assert.deepEqual(imported, []);
});

test("telemetryConsent needs enabled AND a client token", () => {
  assert.equal(telemetryConsent({ telemetry: { enabled: true } }), null);
  assert.equal(telemetryConsent({ telemetry: { posthog_publishable: "phc_x" } }), null);
  assert.deepEqual(
    telemetryConsent({ telemetry: { enabled: true, posthog_publishable: "phc_x", posthog_host: "https://us.i.posthog.com" } }),
    { key: "phc_x", host: "https://us.i.posthog.com" },
  );
});

// ── consented init: exact posthog options (the privacy-load-bearing ones) ────

test("consent → posthog-js imported once, init gets the exact masking options", async () => {
  _resetForTests();
  const fake = fakePosthogModule();
  const imported = [];
  const importer = async (m) => { imported.push(m); return fake.module; };
  const cfg = { telemetry: { enabled: true, posthog_publishable: "phc_x", posthog_host: "https://us.i.posthog.com" } };
  const client = await initTelemetry(cfg, { importer });
  assert.ok(client);
  assert.deepEqual(imported, ["posthog-js"]);
  assert.equal(fake.calls.init.length, 1);
  const [key, options] = fake.calls.init[0];
  assert.equal(key, "phc_x");
  assert.deepEqual(options, {
    api_host: "https://us.i.posthog.com",
    defaults: "2026-05-30",
    session_recording: { maskAllInputs: true, maskTextSelector: ".ph-mask" },
    person_profiles: "never",
  });
  // app_version registered on every event (value best-effort: "unknown" when
  // no server answers /api/version in this test environment).
  assert.equal(fake.calls.register.length, 1);
  const [registered] = fake.calls.register[0];
  assert.equal(typeof registered.app_version, "string");
  assert.ok(registered.app_version.length > 0);

  // screen views carry the lane NAME only
  captureScreen("board");
  assert.deepEqual(fake.calls.capture, [["screen_viewed", { screen: "board" }]]);
  _resetForTests();
});

test("captureScreen before init is a silent no-op", () => {
  _resetForTests();
  assert.doesNotThrow(() => captureScreen("stats"));
});

// ── masking presence: the enumerated anchors carry ph-no-capture ─────────────
// A source-grep test: cheap, honest, and it fails the moment a refactor drops
// a masking class from one of the elements that render operator content.

test("SlideOver masks title, spec, diff and activity log", () => {
  const src = read("SlideOver.jsx");
  for (const anchor of [
    'className="so-title ph-no-capture"',
    'className="so-description ph-no-capture"',
    'className="so-criteria ph-no-capture"',
    'className="so-failure-reason ph-no-capture"',
    'className="so-diff-wrap ph-no-capture"',
  ]) {
    assert.ok(src.includes(anchor), `SlideOver.jsx missing masked anchor: ${anchor}`);
  }
  const feeds = src.match(/className="activity-feed ph-no-capture"/g) || [];
  assert.equal(feeds.length, 2, "both activity-feed containers must be masked");
});

test("Backlog masks the ticket title/assignee block", () => {
  const src = read("Backlog.jsx");
  assert.ok(src.includes('className="flex min-w-0 flex-col gap-1 ph-no-capture"'));
});

test("TaskComposer masks the prompt surface, PR URL and repo row", () => {
  const src = read("TaskComposer.jsx");
  assert.ok(src.includes('sm:p-5 ph-no-capture"'), "prompt surface container");
  assert.ok(src.includes('className="mt-3 ph-no-capture"'), "PR URL container");
  assert.ok(src.includes('gap-2 ph-no-capture"'), "repository row container");
});

test("Board, TaskTable, Stats, App grill and Settings memory are masked", () => {
  assert.ok(read("Board.jsx").includes('"task-card ph-no-capture"'),
    "the whole board card must be masked");
  assert.ok(read("TaskTable.jsx").includes('<tbody className="ph-no-capture">'),
    "the whole table body must be masked");
  const stats = read("Stats.jsx");
  for (const id of ["cost-by-project", "repo-understanding", "session-search"]) {
    assert.ok(
      stats.includes(`className="stats-section ph-no-capture" data-testid="${id}"`),
      `Stats section ${id} must be masked`);
  }
  assert.ok(read("App.jsx").includes('className="grill-spec ph-no-capture"'),
    "the refined-spec block must be masked");
  const settings = read("Settings.jsx");
  assert.ok(settings.includes('className="memory-card ph-no-capture"'));
  assert.ok(settings.includes("memory-card retire-candidate-card ph-no-capture"));
  assert.ok(settings.includes("memory-card learning-card ph-no-capture"));
});

// ── DISCOVERY, not enumeration ───────────────────────────────────────────────
// The 2026-08-16 independent review found Board/TaskTable/Stats leaking task
// titles, repo names and PR URLs while the enumerated anchors above stayed
// green — a guard that enumerates cannot catch the surface nobody listed.
// This sweep DISCOVERS: any component file that renders a known operator-
// content field must carry a ph-no-capture (or ph-mask) annotation, and any
// NEW file that starts rendering one of these fields fails here until it is
// masked (or consciously added to the audited exemptions).

const CONTENT_FIELD_RENDER =
  /\{[a-zA-Z_$][\w$.]*\.(title|task_title|repo_name|pr_url|description|description_short|live_status|blocker_question|failure_reason|snippet|content|path|question|answer|error|test_cmd|summary|query|command|repo|name)\b/;

const SWEEP_EXEMPT = new Set(["telemetry.test.mjs"]);

test("every component rendering operator-content fields carries a mask", async () => {
  const { readdirSync } = await import("node:fs");
  const offenders = [];
  for (const f of readdirSync(SRC)) {
    if (!f.endsWith(".jsx") || SWEEP_EXEMPT.has(f)) continue;
    const src = read(f);
    if (CONTENT_FIELD_RENDER.test(src) &&
        !src.includes("ph-no-capture") && !src.includes("ph-mask")) {
      offenders.push(f);
    }
  }
  assert.deepEqual(offenders, [],
    `files render operator-content fields with no masking annotation: ${offenders}`);
});
