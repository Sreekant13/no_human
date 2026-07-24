// Task-detail drawer, progressive disclosure (task 1.4).
//
// Pure derivations for the summary-first drawer: a plain-language narrative,
// chips, a milestone timeline, and per-section micro-summaries — all computed
// from data the drawer already fetches (`task`, `diff`), never from a raw
// backend enum. Kept out of SlideOver.jsx so the "what does the operator read
// at a glance" logic is node-testable without a DOM.

import { fmtCost, fmtTokens, taskBurn, taskCost } from "./cost.js";
import { formatDuration } from "./formatDuration.js";

// The statuses whose gate the operator clears IN the drawer (Reply / Resume / the
// blocker's options). Single definition — SlideOver's `isParked` and the
// gate-aware default-open-section both read it. Deliberately NOT paused_quota:
// #205 gave it a blocker record and a Resume affordance (rendered via
// decisionFor / the DecisionPanel), but it is a SELF-RESOLVING park — it
// auto-resumes once its subscription quota refreshes — so it is not a
// "waiting on you" gate here.
export const PARKED_STATUSES = new Set(["awaiting_input", "blocked", "escalated"]);

const ACTIVE_STATUSES = new Set([
  "pending", "context", "planning", "implementing", "reviewing", "testing",
]);

// Plain-language stage name for an in-flight status — used both in the
// narrative ("Coder is implementing") and the System section's micro-summary.
export const STATUS_STAGE_LABEL = {
  pending: "starting up",
  context: "gathering context",
  planning: "planning",
  implementing: "implementing",
  reviewing: "reviewing",
  testing: "running tests",
};

// Semantic colour token per status — the SAME palette the board's lanes use
// (boardLanes.js), so the drawer never invents a new meaning for a colour.
export const STATUS_COLOR_VAR = {
  pending: "var(--c-building)",
  context: "var(--c-building)",
  planning: "var(--c-building)",
  implementing: "var(--c-building)",
  reviewing: "var(--c-building)",
  testing: "var(--c-building)",
  awaiting_approval: "var(--c-review)",
  compound_parent: "var(--c-building)",
  awaiting_input: "var(--c-answer)",
  blocked: "var(--c-answer)",
  escalated: "var(--c-escalated)",
  paused_quota: "var(--c-answer)",
  done: "var(--c-done)",
  failed: "var(--c-escalated)",
};

export function colorForStatus(status) {
  return STATUS_COLOR_VAR[status] || "var(--text-muted)";
}

// User-facing label for a task kind — never the raw backend value verbatim
// for kinds that read as code (e.g. "ci_fix" → "CI fix").
const KIND_LABEL = {
  feature: "task",
  bugfix: "bug fix",
  code_review: "code review",
  investigation: "investigation",
  design_doc: "design doc",
  ci_fix: "CI fix",
  compound_parent: "multi-part task",
};

function kindLabel(task) {
  return KIND_LABEL[task?.kind] || "task";
}

// ── Narrative header ───────────────────────────────────────────────────────
// Returns { before, phrase, after, colorVar }: `phrase` is the part that gets
// coloured by the status's semantic token; `before`/`after` are plain text.
// Concatenating before+phrase+after must always read as a complete sentence
// and must NEVER contain a raw backend enum (no underscores, no upper-case
// status tokens) — enforced by slideOverSummary.test.mjs.
export function narrativeFor(task) {
  if (!task) {
    return { before: "Loading task details…", phrase: "", after: "", colorVar: "var(--text-dim)" };
  }
  const kind = kindLabel(task);
  const status = task.status;

  if (status === "done") {
    return { before: `This ${kind} is`, phrase: "done", after: " — its pull request was merged.", colorVar: colorForStatus(status) };
  }
  if (status === "failed") {
    return { before: `This ${kind}`, phrase: "failed", after: " — retry it, or take a closer look at what went wrong.", colorVar: colorForStatus(status) };
  }
  if (status === "awaiting_approval") {
    if (task.approved_at) {
      return { before: `This ${kind} was approved and is`, phrase: "waiting for the PR to merge", after: "", colorVar: colorForStatus(status) };
    }
    return { before: `This ${kind} opened a pull request and is`, phrase: "waiting for your review", after: "", colorVar: colorForStatus(status) };
  }
  if (status === "compound_parent") {
    return { before: `This ${kind} is`, phrase: "coordinating its sub-tasks", after: "", colorVar: colorForStatus(status) };
  }
  if (status === "awaiting_input" || status === "blocked") {
    return { before: `This ${kind} hit a question it can't answer alone —`, phrase: "waiting for your answer", after: "", colorVar: colorForStatus(status) };
  }
  if (status === "escalated") {
    return { before: `This ${kind} couldn't make progress on its own —`, phrase: "waiting for your decision", after: "", colorVar: colorForStatus(status) };
  }
  if (status === "paused_quota") {
    return { before: `This ${kind} is paused —`, phrase: "waiting for its subscription quota to refresh", after: "", colorVar: colorForStatus(status) };
  }
  if (ACTIVE_STATUSES.has(status)) {
    const attempt = task.attempt_count > 0 ? ` — attempt ${task.attempt_count}` : "";
    // Attribution must match the System view's lanes: the Coder is only at the
    // keyboard during `implementing`. Every OTHER stage is a different worker —
    // saying "Coder is planning" while the Coding lane reads "not started yet"
    // is the contradiction this guards against. Name the actual actor per stage.
    if (status === "reviewing") {
      return { before: "The reviewer is", phrase: `checking the work${attempt}`, after: "", colorVar: colorForStatus(status) };
    }
    if (status === "testing") {
      return { before: "Tests are", phrase: `running${attempt}`, after: "", colorVar: colorForStatus(status) };
    }
    if (status === "planning") {
      return { before: "The planner is", phrase: `planning the approach${attempt}`, after: "", colorVar: colorForStatus(status) };
    }
    if (status === "context") {
      return { before: "The orchestrator is", phrase: `gathering context${attempt}`, after: "", colorVar: colorForStatus(status) };
    }
    if (status === "pending") {
      return { before: `This ${kindLabel(task)} is`, phrase: "starting up", after: "", colorVar: colorForStatus(status) };
    }
    const stage = STATUS_STAGE_LABEL[status] || "working";
    return { before: "Coder is", phrase: `${stage}${attempt}`, after: "", colorVar: colorForStatus(status) };
  }
  // Unknown/other status: say so plainly rather than guessing.
  return { before: `This ${kind} is`, phrase: "in an unrecognized state", after: "", colorVar: "var(--text-muted)" };
}

// ── Chips row ──────────────────────────────────────────────────────────────
export function prUrlFor(task) {
  if (!task) return null;
  if (task.context?.pr_url) return task.context.pr_url;
  const attempts = task.attempts || [];
  for (let i = attempts.length - 1; i >= 0; i--) {
    if (attempts[i].pr_url) return attempts[i].pr_url;
  }
  return null;
}

function branchFor(task) {
  const attempts = task?.attempts || [];
  for (let i = attempts.length - 1; i >= 0; i--) {
    if (attempts[i].branch_name) return attempts[i].branch_name;
  }
  return null;
}

// { key, label, sub, href? } — `label` values are the tabular-nums figures;
// `sub` is the quiet caption under them.
export function chipsFor(task) {
  if (!task) return [];
  const chips = [];
  const burn = taskBurn(task);
  const cost = taskCost(task);
  if (burn > 0) chips.push({ key: "cost", label: fmtCost(cost), sub: `${fmtTokens(burn)} tok` });
  if (task.wall_seconds != null) chips.push({ key: "time", label: formatDuration(Math.round(task.wall_seconds)), sub: "wall time" });
  if (task.attempt_count > 0) {
    chips.push({ key: "attempts", label: String(task.attempt_count), sub: task.attempt_count === 1 ? "attempt" : "attempts" });
  }
  const pr = prUrlFor(task);
  if (pr) {
    chips.push({ key: "pr", label: branchFor(task) || "PR", sub: "open pull request", href: pr });
  }
  return chips;
}

// ── Milestone timeline ─────────────────────────────────────────────────────
// created → planned → attempt N → review → PR → done. Derived from `task`
// (created_at/status/attempts/context) — no separate event fetch needed, so
// the summary never blocks on the per-tab lazy event stream.
export function milestonesFor(task) {
  if (!task) return [];
  const attempts = task.attempts || [];
  const last = attempts[attempts.length - 1];
  const hasSpec = !!(task.context?.spec && (task.context.spec.approach || task.context.spec.files_to_change?.length));
  const planned = hasSpec || attempts.length > 0 || !["pending", "context", "planning"].includes(task.status);
  const reviewed = !!last?.review_checklist?.items?.length;
  const pr = !!prUrlFor(task);
  const isDone = task.status === "done";

  const items = [
    { key: "created", label: "Created", done: !!task.created_at },
    { key: "planned", label: "Planned", done: planned },
    { key: "attempt", label: attempts.length ? `Attempt ${attempts.length}` : "Attempt", done: attempts.length > 0 },
    { key: "review", label: "Review", done: reviewed },
    { key: "pr", label: "Pull request", done: pr },
    { key: "done", label: "Done", done: isDone },
  ];

  // The latest milestone reached "pulses" while the task is still active —
  // terminal tasks (done/failed) show a settled trail, nothing pulsing.
  const isTerminal = task.status === "done" || task.status === "failed";
  let lastDoneIdx = -1;
  items.forEach((it, i) => { if (it.done) lastDoneIdx = i; });
  return items.map((it, i) => ({ ...it, current: !isTerminal && i === lastDoneIdx }));
}

// ── Section micro-summaries ────────────────────────────────────────────────
// Single-slot memo keyed on the diff string itself — the drawer shows one
// task's diff at a time, and re-renders (SSE ticks, section toggles) pass the
// SAME string repeatedly, so there's no need to rescan line-by-line each time.
let _diffStatsCache = { diff: undefined, stats: undefined };

function diffStats(diff) {
  if (!diff) return { added: 0, removed: 0, files: 0 };
  if (_diffStatsCache.diff === diff) return _diffStatsCache.stats;
  const files = new Set();
  let added = 0;
  let removed = 0;
  for (const line of diff.split("\n")) {
    if (line.startsWith("diff --git")) {
      const m = line.match(/diff --git a\/(.+?) b\//);
      if (m) files.add(m[1]);
      continue;
    }
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    if (line.startsWith("+")) added++;
    else if (line.startsWith("-")) removed++;
  }
  const stats = { added, removed, files: files.size };
  _diffStatsCache = { diff, stats };
  return stats;
}
export { diffStats };

function relativeTimeFrom(iso, nowMs = Date.now()) {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  const s = Math.max(0, Math.round((nowMs - then) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function reviewMicro(task) {
  const attempts = task.attempts || [];
  const last = attempts[attempts.length - 1];
  const checklist = last?.review_checklist;
  if (!checklist?.items?.length) {
    return { text: "Not reviewed yet", colorVar: "var(--text-muted)" };
  }
  const total = checklist.items.length;
  const failed = checklist.items.filter((it) => !it.passed).length;
  // `checklist.passed` is the backend's authoritative verdict — the SAME value
  // ReviewTab's "Reviewer verdict — PASSED/FAILED" header reads (a stage can
  // fail for reasons an item count alone doesn't show). This micro-summary
  // must never say "All checks passed" while the section it teases says
  // FAILED, so when the two disagree the authoritative flag wins.
  if (failed === 0 && checklist.passed !== false) {
    return { text: "All checks passed", colorVar: "var(--green)" };
  }
  if (failed === 0 && checklist.passed === false) {
    return { text: "Reviewer failed", colorVar: "var(--red)" };
  }
  const prev = attempts[attempts.length - 2]?.review_checklist;
  if (prev?.items?.length === total) {
    const prevPassed = prev.items.filter((it) => it.passed).length;
    const passed = total - failed;
    const fixed = passed - prevPassed;
    if (fixed > 0) {
      return { text: `${failed} finding${failed === 1 ? "" : "s"} · ${fixed} fixed`, colorVar: "var(--red)" };
    }
  }
  return { text: `${failed} finding${failed === 1 ? "" : "s"} · ${total - failed}/${total} passed`, colorVar: "var(--red)" };
}

// Returns { text, colorVar } or null (nothing worth saying yet).
export function sectionSummary(key, { task, diff } = {}) {
  if (!task) return null;
  const attempts = task.attempts || [];
  const last = attempts[attempts.length - 1];
  switch (key) {
    case "system": {
      if (task.status === "done") return { text: "Pipeline complete", colorVar: colorForStatus("done") };
      if (task.status === "failed") return { text: "Pipeline stopped — failed", colorVar: colorForStatus("failed") };
      if (PARKED_STATUSES.has(task.status)) return { text: "Paused, waiting on you", colorVar: colorForStatus(task.status) };
      if (task.status === "paused_quota") return { text: "Paused for quota", colorVar: colorForStatus(task.status) };
      if (task.status === "compound_parent") return { text: "Coordinating sub-tasks", colorVar: colorForStatus(task.status) };
      const stage = STATUS_STAGE_LABEL[task.status];
      return { text: stage ? `Running — ${stage}` : "Running", colorVar: colorForStatus(task.status) };
    }
    case "activity": {
      const ts = task.last_activity || task.updated_at;
      const rel = relativeTimeFrom(ts);
      return { text: rel ? `Last activity ${rel}` : "No activity yet", colorVar: "var(--text-muted)" };
    }
    case "subtasks":
      return { text: "Split into sub-tasks", colorVar: "var(--text-muted)" };
    case "details": {
      // An open question is the more actionable fact — it wins over the
      // criteria count when both are present (the criteria are still one
      // click away inside the section either way).
      if (task.blocker?.question) return { text: "Has a question for you", colorVar: colorForStatus(task.status) };
      const total = task.acceptance_criteria?.length || 0;
      if (total > 0) {
        const done = (task.context?.progress?.acceptance_criteria || [])
          .filter((p) => p?.status === "done").length;
        return { text: `${done}/${total} criteria done`, colorVar: "var(--text-muted)" };
      }
      return { text: "Description & criteria", colorVar: "var(--text-muted)" };
    }
    case "spec": {
      const spec = task.context?.spec;
      const n = spec?.files_to_change?.length;
      return { text: n ? `${n} file${n === 1 ? "" : "s"} planned` : "Not generated yet", colorVar: "var(--text-muted)" };
    }
    case "review":
      return reviewMicro(task);
    case "diff": {
      const { added, removed, files } = diffStats(diff);
      if (!added && !removed) return { text: "No changes yet", colorVar: "var(--text-muted)" };
      return { text: `+${added} −${removed} across ${files} file${files === 1 ? "" : "s"}`, colorVar: "var(--text-muted)" };
    }
    case "attempts": {
      if (!attempts.length) return { text: "No attempts yet", colorVar: "var(--text-muted)" };
      let verdict = null;
      let colorVar = "var(--text-muted)";
      if (last?.review_passed != null) {
        verdict = last.review_passed ? "latest passed" : "issues found";
        colorVar = last.review_passed ? "var(--green)" : "var(--red)";
      }
      return { text: `${attempts.length} attempt${attempts.length === 1 ? "" : "s"}${verdict ? ` · ${verdict}` : ""}`, colorVar };
    }
    default:
      return null;
  }
}

// ── Gate-aware default-open section ────────────────────────────────────────
// Mirrors the pre-1.4 gate-aware first-tab logic: the section that clears
// this task's gate starts open (review's diff+approve, or details' question
// +canned answers); an in-flight task opens on System.
export function defaultOpenSection(task) {
  if (!task) return null;
  if (task.status === "awaiting_approval") return "review";
  if (PARKED_STATUSES.has(task.status)) {
    // A parked task with a blocker now shows the DecisionPanel above the
    // accordion — it carries the question and the actions, so we leave the
    // sections collapsed instead of auto-opening Details onto a wall of
    // description text. Only when there's no blocker to build a panel from do
    // we fall back to opening Details.
    return task.blocker ? null : "details";
  }
  // A compound parent has no pipeline of its own — System renders empty. Open
  // on Sub-tasks when the decomposition actually produced any; otherwise fall
  // back to the pre-existing System default (e.g. mid-decomposition, before
  // subtasks are known).
  if (task.status === "compound_parent") {
    const subtasks = task.context?.decomposition?.subtasks;
    if (Array.isArray(subtasks) && subtasks.length > 0) return "subtasks";
  }
  return "system";
}
