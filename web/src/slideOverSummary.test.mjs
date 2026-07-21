import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  narrativeFor, chipsFor, milestonesFor, sectionSummary, defaultOpenSection,
  diffStats, colorForStatus, PARKED_STATUSES, STATUS_STAGE_LABEL,
} from "./slideOverSummary.js";

const SRC = dirname(fileURLToPath(import.meta.url));

// Derive the status list from the backend enum itself (src/no_human/core/task.py)
// rather than hand-copying it here — a hand-copied list silently drifts when a
// status is added (it did: compound_parent shipped in the backend without ever
// reaching this test). Path is resolved relative to this test file so it works
// regardless of cwd.
const TASK_PY = readFileSync(join(SRC, "../../src/no_human/core/task.py"), "utf8");
const ENUM_BODY_MATCH = TASK_PY.match(/class TaskStatus\(str, Enum\):\n([\s\S]*?)\n\s*\n/);
if (!ENUM_BODY_MATCH) {
  throw new Error("could not locate `class TaskStatus(str, Enum):` body in task.py — did it move/rename?");
}
const ALL_STATUSES = [...ENUM_BODY_MATCH[1].matchAll(/^\s*[A-Z_][A-Z0-9_]*\s*=\s*"([a-z_]+)"/gm)]
  .map((m) => m[1]);

function narrativeText(n) {
  return `${n.before} ${n.phrase} ${n.after}`;
}

// ── narrative: user-side language, never a raw enum ────────────────────────

test("narrative never leaks a raw status/kind enum (no underscores)", () => {
  for (const status of ALL_STATUSES) {
    for (const kind of ["feature", "bugfix", "code_review", "investigation", "design_doc", "ci_fix"]) {
      const task = { status, kind, attempt_count: 2 };
      const text = narrativeText(narrativeFor(task));
      assert.ok(!text.includes("_"), `narrative for ${status}/${kind} leaked an enum: "${text}"`);
    }
  }
});

test("the derived status list matches the backend enum exactly (14 values, includes compound_parent)", () => {
  assert.equal(ALL_STATUSES.length, 14, `expected 14 statuses, derived: ${JSON.stringify(ALL_STATUSES)}`);
  assert.ok(ALL_STATUSES.includes("compound_parent"), "compound_parent must be derived from task.py, not hand-omitted");
});

test("narrativeFor is total over every backend status — never throws, always colors, always says something", () => {
  for (const status of ALL_STATUSES) {
    const task = { status, kind: "feature", attempt_count: 1 };
    const n = narrativeFor(task);
    assert.ok(n.colorVar.startsWith("var(--"), `${status}: narrative colorVar must be a token, got "${n.colorVar}"`);
    assert.notEqual(n.phrase, "", `${status}: narrative phrase must not be empty`);
    assert.doesNotThrow(() => sectionSummary("system", { task }), `${status}: System micro-summary must not throw`);
  }
});

test("compound_parent narrative reads as coordinating sub-tasks, not a raw enum or generic 'Running'", () => {
  const n = narrativeFor({ status: "compound_parent", kind: "compound_parent" });
  assert.match(narrativeText(n), /coordinating/);
  assert.match(n.colorVar, /^var\(--c-(building|answer)\)$/);
  const sys = sectionSummary("system", { task: { status: "compound_parent" } });
  assert.notEqual(sys.text, "Running");
});

test("reviewing/testing attribution copy — not the coder, not a bare stage word", () => {
  const reviewing = narrativeFor({ status: "reviewing" });
  assert.equal(narrativeText(reviewing).includes("Coder is reviewing"), false);
  assert.match(narrativeText(reviewing), /reviewer is checking the work/);

  const testing = narrativeFor({ status: "testing" });
  assert.match(narrativeText(testing), /Tests are running/);
});

test("narrative colors the status phrase by its semantic token", () => {
  const review = narrativeFor({ status: "awaiting_approval" });
  assert.equal(review.colorVar, "var(--c-review)");
  assert.match(review.phrase, /review/);

  const answer = narrativeFor({ status: "awaiting_input" });
  assert.equal(answer.colorVar, "var(--c-answer)");

  const working = narrativeFor({ status: "implementing", attempt_count: 2 });
  assert.equal(working.colorVar, "var(--c-building)");
  assert.match(working.phrase, /implementing/);
  assert.match(working.phrase, /attempt 2/);
});

test("narrative handles the null (loading) task without throwing", () => {
  const n = narrativeFor(null);
  assert.ok(n.before.length > 0);
});

test("approved-but-unmerged reads differently from a fresh PR (B2 #19 parity)", () => {
  const fresh = narrativeFor({ status: "awaiting_approval" });
  const approved = narrativeFor({ status: "awaiting_approval", approved_at: "2026-01-01" });
  assert.notEqual(fresh.phrase, approved.phrase);
  assert.match(approved.phrase, /merge/);
});

// ── chips: cost, wall-time, attempts, PR — tabular data only ───────────────

test("chips include cost+tokens, wall-time, attempts, and a PR link when present", () => {
  const task = {
    total_tokens: 500_000, total_cache_read: 2_000_000, total_cache_creation: 0,
    wall_seconds: 305, attempt_count: 2,
    attempts: [{ branch_name: "nh/task-1", pr_url: null }, { branch_name: "nh/task-1-v2", pr_url: "https://example.com/pr/9" }],
  };
  const chips = chipsFor(task);
  const keys = chips.map((c) => c.key);
  assert.deepEqual(keys, ["cost", "time", "attempts", "pr"]);
  assert.ok(chips[0].label.startsWith("$"));
  assert.equal(chips[2].label, "2");
  assert.equal(chips[3].href, "https://example.com/pr/9");
});

test("chips omit zero/absent fields rather than showing a false 0", () => {
  const chips = chipsFor({ attempt_count: 0, wall_seconds: null });
  assert.deepEqual(chips, []);
});

// ── milestones ──────────────────────────────────────────────────────────────

test("milestones mark created→planned→attempt→review→pr→done in order", () => {
  const task = {
    created_at: "2026-01-01T00:00:00Z", status: "awaiting_approval",
    context: { spec: { approach: "x" }, pr_url: "https://x/pr/1" },
    attempts: [{ review_checklist: { items: [{ passed: true }] } }],
  };
  const m = milestonesFor(task);
  assert.deepEqual(m.map((x) => x.key), ["created", "planned", "attempt", "review", "pr", "done"]);
  assert.equal(m.find((x) => x.key === "done").done, false);
  assert.equal(m.find((x) => x.key === "pr").done, true);
});

test("a done task has no pulsing (current) milestone", () => {
  const task = { created_at: "x", status: "done", attempts: [] };
  const m = milestonesFor(task);
  assert.ok(m.every((x) => x.current === false));
});

test("an active task's LATEST reached milestone pulses", () => {
  const task = { created_at: "x", status: "implementing", attempts: [{ started_at: "x" }] };
  const m = milestonesFor(task);
  const current = m.filter((x) => x.current);
  assert.equal(current.length, 1);
  assert.equal(current[0].key, "attempt");
});

// ── section micro-summaries ───────────────────────────────────────────────

test("review micro-summary never contradicts ReviewTab's authoritative verdict "
  + "(checklist.passed, not a re-derived items count)", () => {
  // Every item.passed is true, but the backend's overall verdict is FAILED
  // (e.g. a stage-level failure an item count doesn't capture) — the tease
  // must say so, not "All checks passed" while the section says FAILED.
  const task = {
    attempts: [{ review_checklist: { passed: false, items: [{ passed: true }, { passed: true }] } }],
  };
  const s = sectionSummary("review", { task });
  assert.equal(s.text, "Reviewer failed");
  assert.notEqual(s.text, "All checks passed");
});

test("review section: findings + fixed-count when a previous attempt regresses to fewer failures", () => {
  const task = {
    status: "reviewing",
    attempts: [
      { review_checklist: { items: [{ passed: false }, { passed: false }, { passed: true }] } },
      { review_checklist: { items: [{ passed: false }, { passed: true }, { passed: true }] } },
    ],
  };
  const s = sectionSummary("review", { task });
  assert.equal(s.text, "1 finding · 1 fixed");
});

test("review section: all-passed reads as a clean summary", () => {
  const task = { attempts: [{ review_checklist: { items: [{ passed: true }, { passed: true }] } }] };
  const s = sectionSummary("review", { task });
  assert.equal(s.text, "All checks passed");
});

test("diff section: additions/deletions/file count, matching the +N -M across K files shape", () => {
  const diff = [
    "diff --git a/foo.js b/foo.js",
    "--- a/foo.js", "+++ b/foo.js",
    "@@ -1,2 +1,3 @@",
    "+line one", "+line two", "-old line",
    "diff --git a/bar.js b/bar.js",
    "--- a/bar.js", "+++ b/bar.js",
    "+added",
  ].join("\n");
  const stats = diffStats(diff);
  assert.equal(stats.added, 3);
  assert.equal(stats.removed, 1);
  assert.equal(stats.files, 2);
  const s = sectionSummary("diff", { task: { status: "reviewing" }, diff });
  assert.equal(s.text, "+3 −1 across 2 files");
});

test("no section micro-summary ever contains a raw status enum", () => {
  const task = {
    status: "awaiting_input", acceptance_criteria: ["a"], blocker: { question: "q?" },
    attempts: [{ review_checklist: { items: [{ passed: false }] } }],
    context: { spec: { files_to_change: ["a.js"] } },
  };
  for (const key of ["system", "activity", "subtasks", "details", "spec", "review", "diff", "attempts"]) {
    const s = sectionSummary(key, { task, diff: "" });
    if (s) assert.ok(!s.text.includes("_"), `${key} micro-summary leaked an enum: "${s.text}"`);
  }
});

// ── gate-aware default section ──────────────────────────────────────────────

test("default-open section maps review-gate/parked/active exactly like the pre-1.4 tab logic", () => {
  assert.equal(defaultOpenSection({ status: "awaiting_approval" }), "review");
  for (const status of PARKED_STATUSES) {
    // No blocker record -> fall back to opening Details (nothing to build a
    // DecisionPanel from).
    assert.equal(defaultOpenSection({ status }), "details");
    // WITH a blocker, the DecisionPanel above the accordion carries the ask, so
    // the sections stay collapsed instead of dumping the description.
    assert.equal(defaultOpenSection({ status, blocker: { category: "X" } }), null);
  }
  assert.equal(defaultOpenSection({ status: "implementing" }), "system");
  assert.equal(defaultOpenSection(null), null);
});

test("compound_parent opens on Sub-tasks when the decomposition produced any", () => {
  const task = {
    status: "compound_parent",
    context: { decomposition: { subtasks: [{ title: "part 1" }, { title: "part 2" }] } },
  };
  assert.equal(defaultOpenSection(task), "subtasks");
});

test("compound_parent falls back to System when no sub-tasks exist yet (empty System pane bug)", () => {
  assert.equal(defaultOpenSection({ status: "compound_parent" }), "system");
  assert.equal(
    defaultOpenSection({ status: "compound_parent", context: { decomposition: { subtasks: [] } } }),
    "system",
  );
  assert.equal(
    defaultOpenSection({ status: "compound_parent", context: {} }),
    "system",
  );
});

test("colorForStatus covers every stage label and every parked status", () => {
  for (const status of Object.keys(STATUS_STAGE_LABEL)) {
    assert.ok(colorForStatus(status).startsWith("var(--"));
  }
  for (const status of PARKED_STATUSES) {
    assert.ok(colorForStatus(status).startsWith("var(--"));
  }
});

// ── static-source checks: the accordion/spacing/motion contract in the actual
//    component + stylesheet (mirrors the themeVars.test.mjs convention — these
//    properties can't be expressed as pure-function unit tests). ─────────────

const slideOverSrc = readFileSync(join(SRC, "SlideOver.jsx"), "utf8");
const stylesCss = readFileSync(join(SRC, "styles.css"), "utf8");

test("SlideOver renders a narrative summary + chips row (not the old tab strip as landing state)", () => {
  assert.match(slideOverSrc, /narrativeFor/);
  assert.match(slideOverSrc, /chipsFor/);
  assert.match(slideOverSrc, /tabular-nums|so-chip/);
});

test("SlideOver renders one accordion section per surviving tab component, closed by default", () => {
  for (const comp of ["SystemTab", "ActivityTab", "DetailsTab", "SpecTab", "ReviewTab", "DiffTab", "AttemptsTab"]) {
    assert.match(slideOverSrc, new RegExp(`<${comp}\\b`), `${comp} must still be rendered as a section body`);
  }
  // Single-open bookkeeping: exactly one state slot tracks which section is open.
  assert.match(slideOverSrc, /openSection/);
});

test("SlideOver uses defaultOpenSection (gate-aware) rather than always defaulting closed", () => {
  assert.match(slideOverSrc, /defaultOpenSection/);
});

test("new --sp-1..8 spacing tokens are defined (theme-independent, like the font tokens)", () => {
  const expected = { 1: "4px", 2: "8px", 3: "12px", 4: "16px", 5: "24px", 6: "32px", 7: "48px", 8: "64px" };
  for (const [n, px] of Object.entries(expected)) {
    assert.match(stylesCss, new RegExp(`--sp-${n}\\s*:\\s*${px}`), `--sp-${n} must be ${px}`);
  }
});

test("accordion expand/collapse animates grid-template-rows with the shared timing tokens, exit faster than enter", () => {
  assert.match(stylesCss, /grid-template-rows:\s*0fr/);
  assert.match(stylesCss, /grid-template-rows:\s*1fr/);
  assert.match(stylesCss, /var\(--dur-base\)/);
  assert.match(stylesCss, /var\(--ease-out\)/);
  // exit ~65% of enter duration
  assert.match(stylesCss, /0\.65/);
});

test("the new motion (accordion + pulse + crossfade) is prefers-reduced-motion guarded", () => {
  const guards = [...stylesCss.matchAll(/@media \(prefers-reduced-motion: reduce\)\s*\{([^]*?)\n\}/g)]
    .map((m) => m[1]).join("\n");
  assert.match(guards, /so-section|so-summary/, "the accordion/summary motion must have a reduced-motion override");
});
