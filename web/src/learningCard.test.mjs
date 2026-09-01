import test from "node:test";
import assert from "node:assert/strict";
import { eventLabel } from "./eventLabels.js";
import {
  BULK_CONFIRM_CAP,
  bulkConfirmIds,
  CORRELATIONAL_LABEL,
  filterLearnings,
  learningEvidence,
  learningOrigin,
  learningOriginTaskId,
  learningScope,
  memoryUsageSummary,
} from "./learningCard.js";

// ── blast radius ─────────────────────────────────────────────────────────── //

test("a row with neither project nor scope is GLOBAL", () => {
  const s = learningScope({ id: "a", project: null, project_scope: null });
  assert.equal(s.kind, "global");
  assert.match(s.label, /every project/);
  // The whole point: the card can render a warning off this without knowing
  // anything about learnings.
  assert.equal(s.detail, null);
});

test("blank strings count as absent, the way SQL NULL does", () => {
  assert.equal(learningScope({ project: "", project_scope: "  " }).kind, "global");
  assert.equal(learningScope({}).kind, "global");
  assert.equal(learningScope(null).kind, "global");
});

test("a scoped row is never called global — including scope-only rows", () => {
  const path = learningScope({ project: "/repo/a", project_scope: null });
  assert.equal(path.kind, "project");
  assert.equal(path.label, "/repo/a");
  assert.equal(path.detail, null);

  // A row that carries only the remote-identity hash HAS a blast radius; it
  // just cannot name a directory. Calling it global would be a false alarm on
  // the one warning that has to stay believable.
  const remote = learningScope({ project: "", project_scope: "prj:" + "a".repeat(64) });
  assert.equal(remote.kind, "project");
  assert.match(remote.label, /remote identity/);
  assert.equal(remote.detail.length, 16, "the hash is truncated like the CLI's");
});

test("both keys present shows the readable path plus the truncated identity", () => {
  const s = learningScope({ project: "/repo/a", project_scope: "prj:" + "b".repeat(64) });
  assert.equal(s.label, "/repo/a");
  assert.equal(s.detail, "prj:bbbbbbbbbbbb");
});

// ── evidence, ported from the CLI ────────────────────────────────────────── //

test("evidence renders the three structured kinds", () => {
  assert.equal(
    learningEvidence({ kind: "supervisor_correction", count: 3, task_ids: ["abcdef123456", "ff"] }),
    "supervisor correction x3 · task(s) abcdef12, ff",
  );
  assert.equal(
    learningEvidence({
      kind: "review_finding", task_id: "0123456789ab", attempt: 2,
      review_round: 1, findings: ["x", "y"],
    }),
    "review finding · task 01234567 · attempt 2 · round 1 · 2 finding(s)",
  );
  assert.equal(
    learningEvidence({ kind: "task_outcome", task_id: "0123456789ab", status: "done" }),
    "task outcome · task 01234567 · done",
  );
});

test("evidence accepts the raw JSON column, not just a parsed object", () => {
  assert.equal(
    learningEvidence('{"kind": "task_outcome", "task_id": "0123456789ab", "status": "done"}'),
    "task outcome · task 01234567 · done",
  );
});

test("evidence is null where the row genuinely records none, and never throws", () => {
  // NULL means unrecorded — pre-B3 rows really do not have this, and inventing
  // a line for them would be the same dishonesty the column's no-default guards.
  assert.equal(learningEvidence(null), null);
  assert.equal(learningEvidence(""), null);
  assert.equal(learningEvidence("{not json"), null);
  assert.equal(learningEvidence("[1,2]"), null);
  assert.equal(learningEvidence({}), null);
  assert.equal(learningEvidence({ kind: "", what: "" }), null);
  // A caption must never be able to blank a card.
  assert.equal(learningEvidence({ kind: "unknown_kind", what: "something odd" }),
    "unknown_kind: something odd");
  assert.ok(learningEvidence({ kind: "k", what: "x".repeat(400) }).length <= 120);
});

test("origin is null rather than guessed for rows that predate the column", () => {
  assert.equal(learningOrigin({ origin: "supervisor" }), "supervisor");
  assert.equal(learningOrigin({ origin: null }), null);
  assert.equal(learningOrigin({}), null);
  assert.equal(learningOrigin(null), null);
});

// ── origin TASK id, for the Second-brain row's task link ────────────────── //

test("learningOriginTaskId reads evidence.task_id, the parsed OR raw-JSON form", () => {
  assert.equal(
    learningOriginTaskId({ evidence: { kind: "task_outcome", task_id: "0123456789ab" } }),
    "0123456789ab");
  assert.equal(
    learningOriginTaskId({ evidence: '{"kind":"task_outcome","task_id":"0123456789ab"}' }),
    "0123456789ab");
});

test("learningOriginTaskId falls back to the first of task_ids, then recurrences", () => {
  assert.equal(
    learningOriginTaskId({ evidence: { kind: "supervisor_correction", task_ids: ["ta", "tb"] } }),
    "ta");
  assert.equal(
    learningOriginTaskId({ evidence: { kind: "review_finding", recurrences: ["tc", "td"] } }),
    "tc");
  // task_id wins over both when present.
  assert.equal(
    learningOriginTaskId({ evidence: { task_id: "t0", task_ids: ["ta"], recurrences: ["tc"] } }),
    "t0");
});

test("learningOriginTaskId is null, never throws, on a row with no recorded task", () => {
  assert.equal(learningOriginTaskId(null), null);
  assert.equal(learningOriginTaskId({}), null);
  assert.equal(learningOriginTaskId({ evidence: null }), null);
  assert.equal(learningOriginTaskId({ evidence: "{not json" }), null);
  assert.equal(learningOriginTaskId({ evidence: "[1,2]" }), null);
  assert.equal(learningOriginTaskId({ evidence: { kind: "task_outcome" } }), null);
});

// ── filter ───────────────────────────────────────────────────────────────── //

const ITEMS = [
  { id: "1", title: "Never push to master", content: "use a branch", type: "rule", origin: "review", project: "/repo/a" },
  { id: "2", title: "Run the tests", content: "before pushing", type: "rule", origin: "history", project: "/repo/b" },
  { id: "3", title: "Kafka topics", content: "mTLS certs", type: "fact", origin: null, project: null },
];

test("an empty query is the identity, not an empty list", () => {
  assert.equal(filterLearnings(ITEMS, ""), ITEMS);
  assert.equal(filterLearnings(ITEMS, "   "), ITEMS);
  assert.deepEqual(filterLearnings(null, "x"), []);
});

test("the filter searches every field the card can show", () => {
  assert.deepEqual(filterLearnings(ITEMS, "master").map((i) => i.id), ["1"]);
  assert.deepEqual(filterLearnings(ITEMS, "pushing").map((i) => i.id), ["2"], "content");
  assert.deepEqual(filterLearnings(ITEMS, "fact").map((i) => i.id), ["3"], "type");
  assert.deepEqual(filterLearnings(ITEMS, "history").map((i) => i.id), ["2"], "origin");
  assert.deepEqual(filterLearnings(ITEMS, "/repo/a").map((i) => i.id), ["1"], "project");
});

test("terms are ANDed so a second word narrows, and matching is case-insensitive", () => {
  assert.deepEqual(filterLearnings(ITEMS, "NEVER MASTER").map((i) => i.id), ["1"]);
  assert.deepEqual(filterLearnings(ITEMS, "never kafka").map((i) => i.id), []);
});

test("a null field never throws the filter", () => {
  assert.deepEqual(filterLearnings(ITEMS, "kafka").map((i) => i.id), ["3"]);
});

// ── bulk confirm ─────────────────────────────────────────────────────────── //

test("bulk confirm sends the selection in display order", () => {
  assert.deepEqual(bulkConfirmIds(ITEMS, new Set(["3", "1"])), ["1", "3"]);
  assert.deepEqual(bulkConfirmIds(ITEMS, ["2"]), ["2"], "an array works like a Set");
  assert.deepEqual(bulkConfirmIds(ITEMS, new Set()), []);
});

test("a selection can never confirm a row the operator cannot see", () => {
  // The safety property. A selection survives in state when the filter
  // changes; without narrowing to the VISIBLE list, "Confirm selected" would
  // activate rules that are not on screen — and for a global rule that is the
  // exact one-click blast radius this module exists to surface.
  const visible = filterLearnings(ITEMS, "kafka");
  assert.deepEqual(bulkConfirmIds(visible, new Set(["1", "2", "3"])), ["3"]);
});

test("the batch is capped, because each confirm is a serialized write", () => {
  const many = Array.from({ length: 200 }, (_, i) => ({ id: `id${i}` }));
  const all = new Set(many.map((m) => m.id));
  assert.equal(bulkConfirmIds(many, all).length, BULK_CONFIRM_CAP);
  assert.equal(bulkConfirmIds(many, all, 3).length, 3);
  assert.deepEqual(bulkConfirmIds(many, all, 0), []);
  assert.deepEqual(bulkConfirmIds(many, all, -5), [], "a negative cap sends nothing");
});

// ── wiring ───────────────────────────────────────────────────────────────── //
//
// Everything above tests a module. A module nothing calls is not a feature, and
// the defect this whole file exists for was precisely that: the DATA was on the
// wire all along (`GET /api/learnings` returns every column) and the client
// threw it away. Static source analysis, the same way settingsOverlay.test.mjs
// and themeVars.test.mjs work — no React renderer is wired into this project's
// `node --test` harness.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const here = fileURLToPath(new URL(".", import.meta.url));
const settingsJsx = readFileSync(here + "Settings.jsx", "utf8");
const stylesCss = readFileSync(here + "styles.css", "utf8");
const slideOverJsx = readFileSync(here + "SlideOver.jsx", "utf8");

test("the learning card actually renders scope, origin and evidence", () => {
  assert.match(settingsJsx, /from "\.\/learningCard\.js"/);
  for (const fn of ["learningScope", "learningOrigin", "learningEvidence"]) {
    assert.match(settingsJsx, new RegExp(`${fn}\\(`), `${fn} is imported but never called`);
  }
  assert.match(settingsJsx, /learning-scope/, "the scope line must be rendered");
});

test("a GLOBAL rule gets a colour rule of its own, and the JSX asks for it", () => {
  // Scoped to what it can actually prove: the class is emitted, and the
  // stylesheet has a rule that colours it. It does NOT prove the colour warns —
  // this assertion was green throughout the one commit on this branch where the
  // rule said `color: var(--amber)`, and --amber is a byte-identical alias of
  // --accent-500, so the badge rendered in ordinary chrome blue. (Inside the
  // branch: neither this rule nor this test exists at main, so nothing blue
  // shipped.) The colour claim belongs where the colour maths lives and is
  // asserted on the RESOLVED hex: contrast.test.mjs, "the GLOBAL blast-radius
  // badge is not painted in the accent colour".
  assert.match(stylesCss, /\.learning-scope\.global\s*\{[^}]*color:/,
    "no colour rule at all for a global rule's blast-radius line");
  assert.match(settingsJsx, /scope\.kind === "global"/);
});

test("nothing is pre-ticked, and bulk confirm is capped and visible-only", () => {
  // `useState(() => new Set())` — an EMPTY selection. The server's own stance
  // (`p["selected"] = False`) exists because a real user was shown their home
  // address already ticked.
  assert.match(settingsJsx, /useState\(\(\)\s*=>\s*new Set\(\)\)/);
  assert.doesNotMatch(settingsJsx, /new Set\((visible|items|pending)\b/,
    "a selection seeded from the list would pre-tick every proposal");
  assert.match(settingsJsx, /bulkConfirmIds\(visible,/,
    "the batch must come from the VISIBLE list, never the unfiltered one");
  assert.match(settingsJsx, /BULK_CONFIRM_CAP/, "the cap must be surfaced to the operator");
});

test("bulk confirm is sequential — the store serialises writes", () => {
  // Firing N confirms at once queues N writes in front of the connection a
  // running task needs.
  //
  // Comments are stripped FIRST. The prose inside that function explains which
  // construct it is avoiding, by name, and the first version of this assertion
  // matched its own explanation and failed — a guard a codebase's own comments
  // can trip is a guard that gets deleted.
  const body = settingsJsx.slice(settingsJsx.indexOf("async function confirmSelected"));
  const fn = body.slice(0, body.indexOf("\n  }") + 4)
    .replace(/(^|[^:])\/\/[^\n]*/g, "$1");
  assert.doesNotMatch(fn, /Promise\.all|Promise\.allSettled/);
  assert.match(fn, /for \(const id of batch\)/);
  assert.match(fn, /await confirmLearning\(id\)/);
});

test("every new class the learning UI renders has a CSS rule", () => {
  // Local version of deadCss.test.mjs's contract, in the other direction: that
  // test catches CSS with no JSX, this catches JSX with no CSS — which renders
  // as unstyled text and reads as "the feature didn't ship".
  for (const cls of ["learning-origin", "learning-scope", "learning-scope-id",
                     "learning-evidence", "learning-select", "learning-toolbar",
                     "learning-filter", "learning-filter-count",
                     "learning-cap-note"]) {
    assert.ok(settingsJsx.includes(cls), `${cls} is styled but never rendered`);
    assert.match(stylesCss, new RegExp(`\\.${cls}[\\s,{:.]`), `.${cls} has no CSS rule`);
  }
  for (const cls of ["rich-knowledge", "rich-knowledge-body", "rich-knowledge-item",
                     "rich-knowledge-why"]) {
    assert.ok(slideOverJsx.includes(cls), `${cls} is styled but never rendered`);
    assert.match(stylesCss, new RegExp(`\\.${cls}[\\s,{:.]`), `.${cls} has no CSS rule`);
  }
});

test("the timeline renders the injected rule titles, not just the count", () => {
  assert.match(slideOverJsx, /kind === "knowledge_accessed"/,
    "knowledge_accessed still falls through to the catch-all one-liner");
  assert.equal(eventLabel("knowledge_accessed"), "Knowledge applied",
    "the event still renders its raw kind as a label");
  const block = slideOverJsx.slice(slideOverJsx.indexOf("function KnowledgeApplied"));
  assert.match(block.slice(0, 2000), /event\.injected/,
    "the injected titles are the whole point");
});

test("the timeline chip uses a button, never <summary>", () => {
  // SlideOver's focus trap (keepFocusInDialog) has neither the `summary`
  // selector nor the collapsed-<details> filter that the Settings overlay had
  // to grow after that markup leaked Tab out of a dialog. Using <details> here
  // would reintroduce a fixed bug in the component that never fixed it.
  const block = slideOverJsx.slice(slideOverJsx.indexOf("function KnowledgeApplied"));
  const fn = block.slice(0, block.indexOf("\n// Render a single event"));
  assert.doesNotMatch(fn, /<summary|<details/);
  assert.match(fn, /<button/);
  assert.match(fn, /aria-expanded/);
});

// ── memory lifecycle A: usage ledger ─────────────────────────────────────── //

test("a memory with no recorded use reports zero, not garbage from missing fields", () => {
  const u = memoryUsageSummary({});
  assert.equal(u.useCount, 0);
  assert.equal(u.lastUsedAt, null);
  assert.equal(u.total, 0);
  assert.equal(u.successPct, 0);
  assert.equal(u.failurePct, 0);
  assert.equal(u.label, CORRELATIONAL_LABEL);
  assert.equal(memoryUsageSummary(null).useCount, 0, "must not throw on null");
});

test("the outcome split is percentages of the LEDGERED total, not use_count", () => {
  // use_count can outrun the ledger total (a task still in flight has an
  // injection row with task_outcome still NULL) — the split must be read off
  // what actually resolved, not off the raw injection count.
  const u = memoryUsageSummary({
    use_count: 10, last_used_at: "2026-08-01T12:00:00",
    success_count: 3, failure_count: 1, cancelled_count: 0, timeout_count: 0,
  });
  assert.equal(u.useCount, 10);
  assert.equal(u.lastUsedAt, "2026-08-01T12:00:00");
  assert.equal(u.total, 4);
  assert.equal(u.successPct, 75);
  assert.equal(u.failurePct, 25);
  assert.equal(u.cancelledPct, 0);
  assert.equal(u.timeoutPct, 0);
});

test("the correlational label is exported, not hand-typed at each call site", () => {
  assert.equal(CORRELATIONAL_LABEL, "Correlational metrics — not causal");
  assert.equal(memoryUsageSummary({}).label, CORRELATIONAL_LABEL);
});

test("both the Rules/Skills card and the Learnings card render the usage row", () => {
  assert.match(settingsJsx, /from "\.\/learningCard\.js"/);
  assert.match(settingsJsx, /memoryUsageSummary\(/, "imported but never called");
  assert.match(settingsJsx, /function MemoryUsageRow/);
  // MemoryCard (rules/skills) AND LearningCard both mount it — a usage row on
  // only one of the two would leave the other panel exactly as blind as
  // before this change.
  const memoryCard = settingsJsx.slice(
    settingsJsx.indexOf("function MemoryCard"),
    settingsJsx.indexOf("function AddMemoryModal"));
  assert.match(memoryCard, /<MemoryUsageRow item={item} \/>/);
  const learningCard = settingsJsx.slice(settingsJsx.indexOf("function LearningCard"));
  assert.match(learningCard, /<MemoryUsageRow item={item} \/>/);
});

test("the usage row and its correlational label carry their own CSS", () => {
  for (const cls of ["memory-usage-row", "memory-usage-empty", "memory-usage-label"]) {
    assert.ok(settingsJsx.includes(cls), `${cls} is styled but never rendered`);
    assert.match(stylesCss, new RegExp(`\\.${cls}[\\s,{:.]`), `.${cls} has no CSS rule`);
  }
});
