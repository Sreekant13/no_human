import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { prefillFromIssue, promptFromIssue, jiraStatusCategory, jiraStatusChipStyle, externalIdFromIssue, importedStatusCategory, importedChip } from "./jiraImport.js";

// Task 1.6 — Import from Jira. Like integrations.test.mjs / settingsOverlay.test.mjs,
// there's no jsdom/React renderer wired into this project's `node --test` harness, so
// the TaskComposer.jsx / App.jsx assertions below read the .jsx source rather than
// mounting components. jiraImport.js's pure functions get real behavioral tests.

const here = fileURLToPath(new URL(".", import.meta.url));
const composerJsx = readFileSync(here + "TaskComposer.jsx", "utf8");
const appJsx = readFileSync(here + "App.jsx", "utf8");
const apiJs = readFileSync(here + "api.js", "utf8");

// ── jiraImport.js — real unit tests ─────────────────────────────────────────

test("prefillFromIssue builds 'KEY: summary' as the title", () => {
  const { title } = prefillFromIssue({
    key: "PROJ-42", summary: "Fix the retry loop", description: "", url: "https://x/browse/PROJ-42",
  });
  assert.equal(title, "PROJ-42: Fix the retry loop");
});

test("prefillFromIssue appends the Jira traceability line to the description", () => {
  const { description } = prefillFromIssue({
    key: "PROJ-1", summary: "S", description: "Body text.", url: "https://acme.atlassian.net/browse/PROJ-1",
  });
  assert.equal(description, "Body text.\n\nImported from Jira: https://acme.atlassian.net/browse/PROJ-1");
});

test("prefillFromIssue still appends the traceability line when the issue has no description", () => {
  const { description } = prefillFromIssue({
    key: "PROJ-2", summary: "S", description: "", url: "https://acme.atlassian.net/browse/PROJ-2",
  });
  assert.equal(description, "\n\nImported from Jira: https://acme.atlassian.net/browse/PROJ-2");
});

test("promptFromIssue joins title+description exactly as splitPrompt.js expects to split them back apart", () => {
  const issue = { key: "PROJ-9", summary: "Do X", description: "Details", url: "https://x/browse/PROJ-9" };
  const prompt = promptFromIssue(issue);
  assert.equal(prompt, "PROJ-9: Do X\n\nDetails\n\nImported from Jira: https://x/browse/PROJ-9");
  // First line only is the title; everything else is description (promptSplit.js's contract).
  const firstLine = prompt.split("\n")[0];
  assert.equal(firstLine, "PROJ-9: Do X");
});

// ── M1: status CATEGORY normalizer + chip style (real behavioral tests) ────

test("jiraStatusCategory maps done/closed/resolved to 'done'", () => {
  assert.equal(jiraStatusCategory("Done"), "done");
  assert.equal(jiraStatusCategory("Closed"), "done");
  assert.equal(jiraStatusCategory("resolved"), "done");
});

test("jiraStatusCategory maps in progress/review to 'active'", () => {
  assert.equal(jiraStatusCategory("In Progress"), "active");
  assert.equal(jiraStatusCategory("In Review"), "active");
  assert.equal(jiraStatusCategory("Review"), "active");
});

test("jiraStatusCategory maps to do/backlog/open/new to 'todo'", () => {
  assert.equal(jiraStatusCategory("To Do"), "todo");
  assert.equal(jiraStatusCategory("Backlog"), "todo");
  assert.equal(jiraStatusCategory("Open"), "todo");
  assert.equal(jiraStatusCategory("New"), "todo");
});

test("jiraStatusCategory falls back to 'unknown' for a custom workflow status", () => {
  assert.equal(jiraStatusCategory("Blocked on Vendor"), "unknown");
  assert.equal(jiraStatusCategory(""), "unknown");
  assert.equal(jiraStatusCategory(undefined), "unknown");
});

test("a Done ticket and a To Do ticket get different chip classes/colours", () => {
  const done = jiraStatusChipStyle("Done");
  const todo = jiraStatusChipStyle("To Do");
  assert.notEqual(done, null);
  assert.notEqual(todo, null);
  assert.notEqual(done.color, todo.color);
  assert.notEqual(done.background, todo.background);
});

test("jiraStatusChipStyle returns null for unknown status (caller keeps its neutral className)", () => {
  assert.equal(jiraStatusChipStyle("Some Custom Status"), null);
});

test("chip tokens are the EXISTING bridged semantic ramp, never a raw hex", () => {
  for (const status of ["Done", "In Progress", "To Do"]) {
    const style = jiraStatusChipStyle(status);
    for (const value of Object.values(style)) {
      assert.match(value, /^var\(--[a-z-]+\)$/, `${status} chip style must reference a CSS var, got ${value}`);
    }
  }
});

test("TaskComposer.jsx derives the chip style from jiraStatusChipStyle, not a static class", () => {
  assert.match(composerJsx, /import \{ promptFromIssue, jiraStatusChipStyle, externalIdFromIssue, importedChip \} from ["']\.\/jiraImport\.js["']/);
  assert.match(composerJsx, /const chipStyle = jiraStatusChipStyle\(issue\.status\);/);
  // The neutral fallback (unknown status) still reads via className, not colour alone.
  assert.match(composerJsx, /border-line bg-panel text-text-muted/);
});

// ── M2: focus returns to the prompt textarea after a pick ───────────────────

test("a promptRef is defined and attached to the prompt textarea", () => {
  assert.match(composerJsx, /const promptRef = useRef\(null\);/);
  assert.match(composerJsx, /<textarea\s*\n\s*ref=\{promptRef\}/);
});

test("handleJiraPick focuses the prompt textarea (guarded against a null ref)", () => {
  const fn = composerJsx.match(/function handleJiraPick\(issue\)\s*\{[\s\S]*?\n  \}/)?.[0];
  assert.ok(fn, "handleJiraPick must be found");
  assert.match(fn, /promptRef\.current\?\.focus\(\);/);
});

// ── api.js — the new read call + source passthrough ────────────────────────

test("searchJiraIssues hits the new read endpoint with q + limit", () => {
  assert.match(apiJs, /export async function searchJiraIssues\(q, limit = 20\)/);
  assert.match(apiJs, /\/api\/integrations\/jira\/issues\?\$\{params\}/);
});

test("createTask forwards source (undefined for every typed task, unchanged wire shape)", () => {
  assert.match(apiJs, /export async function createTask\(\{[^}]*\bsource\b[^}]*\}\)/s);
  assert.match(apiJs, /JSON\.stringify\(\{ title, description, repo_path, project_id, kind, priority, acceptance_criteria, source, external_id \}\)/);
});

test("createTask no longer sends a backend field (single in-process Claude backend; no picker)", () => {
  assert.doesNotMatch(apiJs, /createTask\([^)]*\bbackend\b/s);
});

// ── TaskComposer.jsx — the affordance + panel ───────────────────────────────

test("the affordance is gated on jiraConfigured and reads 'Import from Jira'", () => {
  assert.match(composerJsx, /\{jiraConfigured\s*&&\s*\(/, "must be conditionally rendered on jiraConfigured");
  assert.match(composerJsx, /Import from Jira/);
});

test("jiraConfigured is derived from a one-time fetchIntegrations call, never a raw config poke", () => {
  assert.match(composerJsx, /fetchIntegrations\(\)/);
  assert.match(composerJsx, /\.find\(\(i\)\s*=>\s*i\.name\s*===\s*["']jira["']\)/);
  assert.match(composerJsx, /setJiraConfigured\(Boolean\(jira\?\.configured\)\)/);
  // Fetched once at mount ([] deps), not on every render.
  assert.match(composerJsx, /fetchIntegrations\(\)[\s\S]{0,400}\}, \[\]\);/);
});

test("typing is debounced 300ms; the empty browse loads immediately", () => {
  assert.match(composerJsx, /setTimeout\(\(\) => \{[\s\S]*?searchJiraIssues\(q\)/);
  // Typing waits 300ms; an empty query (browse-all on open) loads with no delay.
  assert.match(composerJsx, /\}, q \? 300 : 0\);/);
});

test("a stale response is discarded via an ignore flag + cleanup timer (race guard)", () => {
  assert.match(composerJsx, /let ignore = false;/);
  assert.match(composerJsx, /return \(\) => \{ ignore = true; clearTimeout\(h\); \};/);
});

test("picking an issue prefills the prompt via promptFromIssue and sets the hidden source marker", () => {
  assert.match(composerJsx, /function handleJiraPick\(issue\)\s*\{/);
  assert.match(composerJsx, /setPrompt\(promptFromIssue\(issue\)\)/);
  assert.match(composerJsx, /setSource\(["']jira["']\)/);
  // Panel closes and the search resets after a pick.
  assert.match(composerJsx, /setJiraOpen\(false\);\s*\n\s*setJiraQuery\(""\);/);
});

// ── SCRUM-9: the picker must fetch the FULL issue before prefilling ────────
// (the browse list's brief truncates description to 2000 chars — the created
// task must carry the whole spec, not that cut-off list-view text).

test("handleJiraPick is async and re-fetches the picked issue in full via fetchJiraIssue", () => {
  assert.match(composerJsx, /import \{ fetchProjects, fetchConfig, fetchIntegrations, searchJiraIssues, fetchJiraIssue \} from ["']\.\/api\.js["']/);
  assert.match(composerJsx, /async function handleJiraPick\(issue\)\s*\{/);
  const fn = composerJsx.match(/async function handleJiraPick\(issue\)\s*\{[\s\S]*?\n  \}/)?.[0];
  assert.ok(fn, "handleJiraPick must be found");
  assert.match(fn, /issue = await fetchJiraIssue\(issue\.key\);/);
  // Still prefills via promptFromIssue using the (now full) issue, and still
  // focuses the textarea afterwards.
  assert.match(fn, /setPrompt\(promptFromIssue\(issue\)\);/);
  assert.match(fn, /promptRef\.current\?\.focus\(\);/);
});

test("api.js exposes fetchJiraIssue hitting the single-issue detail endpoint", () => {
  assert.match(apiJs, /export async function fetchJiraIssue\(key\)/);
  assert.match(apiJs, /\/api\/integrations\/jira\/issues\/\$\{encodeURIComponent\(key\)\}/);
});

test("source defaults to 'board' and survives a re-seeded composer (grill-fail echo)", () => {
  assert.match(composerJsx, /const \[source, setSource\] = useState\(initial\?\.source \?\? "board"\);/);
});

test("source rides along in the onStart payload alongside the other echoed fields", () => {
  assert.match(composerJsx, /prompt,\s*\n\s*prUrl,\s*\n\s*customRepo,\s*\n\s*source,\s*\n\s*externalId,\s*\n\s*\}\);/);
});

test("empty, loading and error states use the design-direction copy/patterns", () => {
  assert.match(composerJsx, /No matching tickets\./);
  assert.match(composerJsx, /className="skeleton /, "must use the shared shimmer skeleton, not a spinner");
  assert.match(composerJsx, /role="alert"/);
  assert.match(composerJsx, /Try again/);
  assert.match(composerJsx, /aria-live="polite"/);
});

// ── Grill flow stays untouched ───────────────────────────────────────────

test("TaskComposer never defines or calls grill machinery directly — it only hands off via onStart", () => {
  for (const forbidden of [/function startGrill/, /_grillParams/, /_startGrillSSE/, /grillStepSSE/, /grillStep\(/]) {
    assert.doesNotMatch(composerJsx, forbidden, `grill logic must stay in App.jsx, not TaskComposer.jsx: ${forbidden}`);
  }
});

test("App.jsx's grill request body is untouched — source never leaks into /api/grill", () => {
  assert.match(appJsx, /function _grillParams\(qaOverride, spec = fields\)\s*\{/);
  assert.match(
    appJsx,
    /return \{\s*\n\s*title: spec\.title, description: spec\.description,[\s\S]*?qa_history: qaOverride \?\? \[\],\s*\n\s*\};/,
    "the grill request shape must stay exactly title/description/repo_path/project_id/qa_history",
  );
});

test("App.jsx's createTask call carries fields.source through, on top of the untouched fields", () => {
  assert.match(appJsx, /source: fields\.source,/);
});

// ── SCRUM-33: external_id dedup — picker sends the Jira key; typed tasks don't ──

test("externalIdFromIssue returns the issue key (the dedup id sent to the backend)", () => {
  assert.equal(externalIdFromIssue({ key: "PROJ-42", summary: "Fix the retry loop" }), "PROJ-42");
});

test("externalIdFromIssue returns null with no key (mutation-proof, not a tautology)", () => {
  assert.equal(externalIdFromIssue({}), null);
  assert.equal(externalIdFromIssue(undefined), null);
});

test("handleJiraPick sets externalId from the picked issue via externalIdFromIssue", () => {
  const fn = composerJsx.match(/async function handleJiraPick\(issue\)\s*\{[\s\S]*?\n  \}/)?.[0];
  assert.ok(fn, "handleJiraPick must be found");
  assert.match(fn, /setExternalId\(externalIdFromIssue\(issue\)\);/);
});

test("App.jsx's createTask call and api.js's wire body both carry external_id", () => {
  assert.match(appJsx, /external_id: fields\.externalId,/);
  assert.match(apiJs, /export async function createTask\(\{[^}]*\bexternal_id\b[^}]*\}\)/s);
  assert.match(apiJs, /JSON\.stringify\(\{[^}]*\bexternal_id\b[^}]*\}\)/s);
});

test("externalId defaults to null and ONLY handleJiraPick ever sets it (typed/board tasks never do)", () => {
  assert.match(composerJsx, /const \[externalId, setExternalId\] = useState\(initial\?\.externalId \?\? null\);/);
  const totalSetters = (composerJsx.match(/setExternalId\(/g) || []).length;
  const fn = composerJsx.match(/async function handleJiraPick\(issue\)\s*\{[\s\S]*?\n  \}/)?.[0];
  assert.ok(fn, "handleJiraPick must be found");
  const settersInPick = (fn.match(/setExternalId\(/g) || []).length;
  assert.equal(settersInPick, 1, "handleJiraPick must set externalId exactly once");
  assert.equal(
    totalSetters,
    settersInPick,
    "setExternalId must appear ONLY inside handleJiraPick — no typed/board path may set it",
  );
});

// ── SCRUM-18: accidental re-import trap — the "imported" chip ──────────────

test("importedStatusCategory maps 'done' to 'done'", () => {
  assert.equal(importedStatusCategory("done"), "done");
});

test("importedStatusCategory maps in-flight board statuses to 'active'", () => {
  assert.equal(importedStatusCategory("implementing"), "active");
  assert.equal(importedStatusCategory("pending"), "active");
  assert.equal(importedStatusCategory("awaiting_approval"), "active");
});

test("importedStatusCategory maps failed/blocked/escalated to 'warn'", () => {
  assert.equal(importedStatusCategory("failed"), "warn");
  assert.equal(importedStatusCategory("blocked"), "warn");
  assert.equal(importedStatusCategory("escalated"), "warn");
});

test("importedStatusCategory falls back to 'unknown' for an unrecognised value", () => {
  assert.equal(importedStatusCategory("something-new"), "unknown");
  assert.equal(importedStatusCategory(""), "unknown");
});

test("importedChip returns null when the ticket has no board task yet", () => {
  assert.equal(importedChip(null), null);
  assert.equal(importedChip(undefined), null);
});

test("importedChip labels a matched ticket with its board status and a non-null style", () => {
  const chip = importedChip({ task_id: "abc123", status: "done", count: 1 });
  assert.ok(chip);
  assert.match(chip.label, /imported/);
  assert.match(chip.label, /done/);
  assert.notEqual(chip.style, null);
});

test("importedChip still returns a chip for an in-progress board task (import stays possible, state visible)", () => {
  const chip = importedChip({ task_id: "abc123", status: "implementing", count: 1 });
  assert.ok(chip);
  assert.match(chip.label, /implementing/);
});

test("importedChip escalates to a review warning when count > 1 (duplicate external_ids)", () => {
  const chip = importedChip({ task_id: "abc123", status: "done", count: 2 });
  assert.ok(chip);
  assert.match(chip.label, /review|warn/i);
  assert.match(chip.label, /2/);
});

test("importedChip style references only bridged CSS vars, never a raw hex", () => {
  for (const imported of [
    { task_id: "a", status: "done", count: 1 },
    { task_id: "b", status: "failed", count: 1 },
    { task_id: "c", status: "done", count: 3 },
  ]) {
    const style = importedChip(imported).style;
    if (style) {
      for (const value of Object.values(style)) {
        assert.match(value, /^var\(--[a-z-]+\)$/, `style must reference a CSS var, got ${value}`);
      }
    }
  }
});

test("TaskComposer.jsx renders the imported chip from issue.imported next to the Jira status chip", () => {
  assert.match(
    composerJsx,
    /import \{ promptFromIssue, jiraStatusChipStyle, externalIdFromIssue, importedChip \} from ["']\.\/jiraImport\.js["']/,
  );
  assert.match(composerJsx, /const imp = importedChip\(issue\.imported\);/);
  assert.match(composerJsx, /\{imp && \(/);
});

test("TaskComposer.jsx never disables the picker row button — import stays possible when imported", () => {
  const rowButton = composerJsx.match(/<button\s*\n\s*key=\{issue\.key\}[\s\S]*?<\/button>/)?.[0];
  assert.ok(rowButton, "picker row button must be found");
  assert.doesNotMatch(rowButton, /disabled/);
  // The onClick handler is unconditional — importedness never gates the pick.
  assert.match(rowButton, /onClick=\{\(\) => handleJiraPick\(issue\)\}/);
});

test("external_id_survives_grill_reseed_roundtrip: initial.externalId seeds state and onStart echoes the identical camelCase token", () => {
  // Seed side: a re-mount from `initial` (the grill-fail re-seed path) reads externalId.
  assert.match(composerJsx, /const \[externalId, setExternalId\] = useState\(initial\?\.externalId \?\? null\);/);
  // Echo side: onStart emits the SAME camelCase token, so the next re-seed's `initial`
  // (== a prior onStart payload) round-trips it. A name mismatch on either end is
  // exactly the bug the parked attempt's reviewer flagged.
  assert.match(composerJsx, /prompt,\s*\n\s*prUrl,\s*\n\s*customRepo,\s*\n\s*source,\s*\n\s*externalId,\s*\n\s*\}\);/);
});
