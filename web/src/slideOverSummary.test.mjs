import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  narrativeFor, chipsFor, milestonesFor, sectionSummary, defaultOpenSection,
  diffStats, colorForStatus, PARKED_STATUSES, STATUS_STAGE_LABEL, isTerminalStatus,
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

test("paused_quota reads as a self-resolving quota park, not a task-budget wait", () => {
  // paused_quota comes from _park_quota (subscription QUOTA exhausted), and it
  // auto-resumes when quota refreshes — distinct from BUDGET_EXHAUSTED (the
  // per-task token budget). Neither the narrative nor the badge may call it "budget".
  const text = narrativeText(narrativeFor({ status: "paused_quota", kind: "task" }));
  assert.match(text, /quota/i, "narrative names the subscription quota");
  assert.doesNotMatch(text, /budget/i, "narrative must not conflate quota with the task budget");
  const badge = sectionSummary("system", { task: { status: "paused_quota" } });
  assert.match(badge.text, /quota/i, "badge names quota");
  assert.doesNotMatch(badge.text, /budget/i, "badge must not say budget");
});

test("reviewing/testing attribution copy — not the coder, not a bare stage word", () => {
  const reviewing = narrativeFor({ status: "reviewing", claimed: true });
  assert.equal(narrativeText(reviewing).includes("Coder is reviewing"), false);
  assert.match(narrativeText(reviewing), /reviewer is checking the work/);

  const testing = narrativeFor({ status: "testing", claimed: true });
  assert.match(narrativeText(testing), /Tests are running/);
});

test("pre-coding stages are attributed to the real actor, never the Coder", () => {
  // The Coder is only at the keyboard during `implementing`. Saying "Coder is
  // planning" while the System view's Coding lane reads "not started yet" is
  // the contradiction this guards. Each stage must name the actual worker.
  const planning = narrativeFor({ status: "planning", claimed: true });
  assert.equal(narrativeText(planning).includes("Coder is"), false, "planning must not credit the Coder");
  assert.match(narrativeText(planning), /planner is planning the approach/);

  const context = narrativeFor({ status: "context", claimed: true });
  assert.equal(narrativeText(context).includes("Coder is"), false, "context must not credit the Coder");
  assert.match(narrativeText(context), /orchestrator is gathering context/);

  const pending = narrativeFor({ status: "pending" });
  assert.equal(narrativeText(pending).includes("Coder is"), false, "pending must not credit the Coder");
  assert.match(narrativeText(pending), /starting up/);

  // The one stage the Coder IS active must still say so — no over-correction.
  const implementing = narrativeFor({ status: "implementing", claimed: true });
  assert.match(narrativeText(implementing), /Coder is implementing/);
});

test("narrative colors the status phrase by its semantic token", () => {
  const review = narrativeFor({ status: "awaiting_approval" });
  assert.equal(review.colorVar, "var(--c-review)");
  assert.match(review.phrase, /review/);

  const answer = narrativeFor({ status: "awaiting_input" });
  assert.equal(answer.colorVar, "var(--c-answer)");

  const working = narrativeFor({ status: "implementing", attempt_count: 2, claimed: true });
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

test("a human-stopped blocked task reads as parked by the human, not waiting for an answer", () => {
  const stopped = narrativeFor({ status: "blocked", blocker: { human_stopped: true } });
  const waiting = narrativeFor({ status: "blocked" });
  assert.notEqual(stopped.phrase, waiting.phrase);
  assert.doesNotMatch(narrativeText(stopped), /waiting for your answer/);
  assert.match(narrativeText(stopped), /parked|stopped/);
});

test("the flattened board field (blocker_human_stopped) reads the same as the full blocker object", () => {
  const stopped = narrativeFor({ status: "blocked", blocker_human_stopped: true });
  assert.match(narrativeText(stopped), /parked|stopped/);
  assert.doesNotMatch(narrativeText(stopped), /waiting for your answer/);
});

test("a human-stopped ESCALATED task also reads as parked, not 'waiting for your decision' "
  + "(human_stopped is stamped on any parked status, not just blocked/awaiting_input)", () => {
  const stopped = narrativeFor({ status: "escalated", blocker: { human_stopped: true } });
  const waiting = narrativeFor({ status: "escalated" });
  assert.notEqual(stopped.phrase, waiting.phrase);
  assert.doesNotMatch(narrativeText(stopped), /waiting for your decision/);
  assert.match(narrativeText(stopped), /parked|stopped/);
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

// ── details micro: never a phantom "0/N criteria done" ─────────────────────

test("details micro: passed-review task with untracked criteria shows Not tracked, never 0/N", () => {
  const task = {
    status: "awaiting_approval",
    acceptance_criteria: ["a", "b"],
    // review_passed comes across the wire as an int (0/1), not a boolean —
    // see api/models.py `review_passed: int | None`.
    attempts: [{ review_passed: 1 }],
  };
  const s = sectionSummary("details", { task });
  assert.equal(s.text, "Not tracked");
});

test("details micro: done task with untracked criteria shows Not tracked, never 0/N", () => {
  const task = { status: "done", acceptance_criteria: ["a", "b", "c"] };
  const s = sectionSummary("details", { task });
  assert.equal(s.text, "Not tracked");
  assert.ok(!s.text.includes("0/"), `must never show a phantom 0/N, got "${s.text}"`);
});

test("details micro: passed/done task with tracked criteria shows the real count", () => {
  const task = {
    status: "done",
    acceptance_criteria: ["a", "b"],
    context: { progress: { acceptance_criteria: [{ status: "done" }, { status: "done" }] } },
  };
  const s = sectionSummary("details", { task });
  assert.equal(s.text, "2/2 criteria done");
});

test("details micro: mid-flight task with untracked criteria shows Not tracked, never a phantom 0/N", () => {
  const task = { status: "coding", acceptance_criteria: ["a", "b"] };
  const s = sectionSummary("details", { task });
  assert.equal(s.text, "Not tracked");
});

test("details micro: mid-flight task with partially tracked criteria shows the real count", () => {
  const task = {
    status: "coding",
    acceptance_criteria: ["a", "b", "c"],
    context: { progress: { acceptance_criteria: [{ status: "done" }] } },
  };
  const s = sectionSummary("details", { task });
  assert.equal(s.text, "1/3 criteria done");
});

// ── SCRUM-80: terminal tasks neutralize a live blocker ask ─────────────────

test("isTerminalStatus is true only for done/failed", () => {
  assert.equal(isTerminalStatus("done"), true);
  assert.equal(isTerminalStatus("failed"), true);
  assert.equal(isTerminalStatus("blocked"), false);
  assert.equal(isTerminalStatus("awaiting_input"), false);
  assert.equal(isTerminalStatus("implementing"), false);
});

test("details micro neutralizes a blocker question once the task is terminal (failed)", () => {
  const s = sectionSummary("details", { task: { status: "failed", blocker: { question: "why?" } } });
  assert.equal(s.text, "Asked before it ended");
  assert.equal(s.colorVar, "var(--text-muted)");
});

test("details micro neutralizes a blocker question once the task is terminal (done)", () => {
  const s = sectionSummary("details", { task: { status: "done", blocker: { question: "why?" } } });
  assert.equal(s.text, "Asked before it ended");
  assert.equal(s.colorVar, "var(--text-muted)");
});

test("details micro keeps the live ask for a non-terminal (parked) blocker", () => {
  const s = sectionSummary("details", { task: { status: "blocked", blocker: { question: "why?" } } });
  assert.equal(s.text, "Has a question for you");
  assert.equal(s.colorVar, colorForStatus("blocked"));
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

test("SCRUM-80: blocker-history modifier is wired in both the component and the stylesheet", () => {
  assert.match(slideOverSrc, /blocker-history/, "SlideOver.jsx must apply the blocker-history class for terminal tasks");
  const rulesMatch = stylesCss.match(/\.blocker-history[^{]*\{[^}]*\}/g);
  assert.ok(rulesMatch && rulesMatch.length > 0, "styles.css must define .blocker-history rules");
  const rulesText = rulesMatch.join("\n");
  // No new/invented tokens and no hardcoded hex — only the existing neutral
  // tokens already defined in both :root and [data-theme="light"] (checked
  // directly below, against both theme blocks parsed from stylesCss).
  assert.doesNotMatch(rulesText, /#[0-9a-fA-F]{3,8}\b/, ".blocker-history must not hardcode a hex color");
  const usedVars = [...rulesText.matchAll(/var\((--[a-z-]+)\)/g)].map((m) => m[1]);
  assert.ok(usedVars.length > 0, ".blocker-history must reference at least one CSS var");
  const allowed = new Set(["--text-dim", "--text-muted", "--border", "--border-hi", "--text"]);
  for (const v of usedVars) {
    assert.ok(allowed.has(v), `.blocker-history uses an unexpected token ${v} — must reuse an existing neutral token`);
  }
  const rootBlock = stylesCss.match(/:root\s*\{([^}]*)\}/)?.[1] || "";
  const lightBlock = stylesCss.match(/\[data-theme="light"\]\s*\{([^}]*)\}/)?.[1] || "";
  for (const v of allowed) {
    assert.match(rootBlock, new RegExp(`${v}\\s*:`), `${v} must be defined in :root`);
    assert.match(lightBlock, new RegExp(`${v}\\s*:`), `${v} must be defined in [data-theme="light"]`);
  }
});

test("the new motion (accordion + pulse + crossfade) is prefers-reduced-motion guarded", () => {
  const guards = [...stylesCss.matchAll(/@media \(prefers-reduced-motion: reduce\)\s*\{([^]*?)\n\}/g)]
    .map((m) => m[1]).join("\n");
  assert.match(guards, /so-section|so-summary/, "the accordion/summary motion must have a reduced-motion override");
});

// ── SCRUM-16: ACTIVE means a live claimed session ──────────────────────────

test("unclaimed active task narrates as queued, never as an actor working", () => {
  for (const status of ["context", "planning", "implementing", "reviewing", "testing"]) {
    const n = narrativeFor({ status, kind: "feature", attempt_count: 2, claimed: false });
    const text = `${n.before} ${n.phrase}`;
    assert.match(text, /queued/i, `${status} unclaimed must read queued`);
    assert.doesNotMatch(text, /Coder is|reviewer is|planner is|orchestrator is|Tests are/i,
      `${status} unclaimed must not credit a live actor`);
    assert.match(n.phrase, /attempt 2/, "attempt number survives");
  }
});

test("claimed active task keeps today's actor attribution", () => {
  const n = narrativeFor({ status: "implementing", kind: "feature", attempt_count: 1, claimed: true });
  assert.match(`${n.before} ${n.phrase}`, /Coder is implementing/);
  const r = narrativeFor({ status: "reviewing", kind: "feature", claimed: true });
  assert.match(`${r.before} ${r.phrase}`, /reviewer is checking/i);
});

test("parked/terminal narratives are unchanged by the claimed field", () => {
  const e = narrativeFor({ status: "escalated", kind: "feature", claimed: false });
  assert.match(`${e.before} ${e.phrase}`, /waiting for your decision/);
  const d = narrativeFor({ status: "done", kind: "feature", claimed: false });
  assert.ok(d.phrase.length > 0);
});
