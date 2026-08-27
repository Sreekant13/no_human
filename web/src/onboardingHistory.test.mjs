import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { scanSummary, groupProposalsByProject } from "./onboardingHistory.js";

// Measured on a live wizard run with an empty home, and it is the whole reason
// this module exists:
//
//     Scanned 0 conversations → 16 items to review (incl. 16 skills).
//
// Zero conversations, sixteen findings, one sentence — and the "16 skills" was
// the number found on disk rather than the number queued, so a re-scan (which
// dedupes) would have printed the same sixteen beside "0 items to review".

const SRC = dirname(fileURLToPath(import.meta.url));
const onboarding = readFileSync(join(SRC, "Onboarding.jsx"), "utf8");

test("with no conversations, the count is attributed to what it came from", () => {
  const s = scanSummary({ transcripts: 0, messages: 0, skills: 16, proposals: 16 });
  assert.doesNotMatch(s, /Scanned 0/,
    "a zero must not be printed next to a non-zero total as though it produced it");
  assert.match(s, /16 skills/);
  assert.match(s, /16 items to review/);
});

test("with nothing found at all, it says so without inventing a source", () => {
  const s = scanSummary({ transcripts: 0, skills: 0, proposals: 0 });
  assert.match(s, /No past conversations were readable/);
  assert.doesNotMatch(s, /skill/, "no skills were cataloged, so none are claimed");
});

test("with conversations, it reports the pass that actually ran", () => {
  const s = scanSummary({ transcripts: 12, messages: 3400, skills: 2, proposals: 7 });
  assert.match(s, /Scanned 12 conversations/);
  assert.match(s, /3,400 messages/);
  assert.match(s, /7 items to review/);
  assert.match(s, /2 skills/);
});

test("singulars are singular", () => {
  const s = scanSummary({ transcripts: 1, messages: 4, skills: 1, proposals: 1 });
  assert.match(s, /1 conversation \(/);
  assert.match(s, /1 item to review/);
  assert.match(s, /1 skill\b/);
});

test("the Claude Code split is named only when it is not the whole set", () => {
  assert.match(scanSummary({ transcripts: 5, proposals: 1, claudeCode: 2 }),
    /2 from Claude Code/);
  assert.doesNotMatch(scanSummary({ transcripts: 5, proposals: 1, claudeCode: 5 }),
    /from Claude Code/, "saying '5 of 5' adds nothing but length");
});

// ── proposals split into the selected repos vs other projects (spec §3 B5) ──

test("proposals group by whether their project is inside a selected repo", () => {
  const proposals = [
    { id: "a", project: "/Users/u/mine/x" },   // inside → inScope
    { id: "b", project: "/Users/u/other" },     // other project
    { id: "c", project: "/Users/u/mine-other" },// prefix collision, NOT inside
    { id: "d" },                                 // no project (skill) → inScope
  ];
  const { inScope, other } = groupProposalsByProject(proposals, ["/Users/u/mine"]);
  assert.deepEqual(inScope.map((p) => p.id), ["a", "d"]);
  const byProject = Object.fromEntries(other.map((g) => [g.project, g.items.map((i) => i.id)]));
  assert.deepEqual(byProject, {
    "/Users/u/other": ["b"],
    "/Users/u/mine-other": ["c"],
  });
});

test("with no repos selected, every proposal is in scope", () => {
  const { inScope, other } = groupProposalsByProject(
    [{ id: "a", project: "/x" }, { id: "b", project: "/y" }], []);
  assert.equal(inScope.length, 2);
  assert.equal(other.length, 0);
});

test("a trailing slash on a selected repo does not change containment", () => {
  const { inScope, other } = groupProposalsByProject(
    [{ id: "a", project: "/Users/u/mine/x" }], ["/Users/u/mine/"]);
  assert.deepEqual(inScope.map((p) => p.id), ["a"]);
  assert.equal(other.length, 0);
});

// ── the step states its scope and sends the selected repos ─────────────────

test("the history scope copy states the selected-repo scope, and the scan sends the repos", () => {
  assert.match(onboarding, /Scope: conversations from the last <strong>30 days<\/strong> in/);
  assert.match(onboarding, /selected repo/);
  assert.match(onboarding, /analyzeHistory\(30, \[\.\.\.selectedRepos\]\)/,
    "the scan must scope the analyze call to the selected repos");
});

// ── the step must render the numbers from the pass it is describing ────────

test("the result callout is built from the ANALYZE response, not the extract probe", () => {
  assert.match(onboarding, /scanPhase === "done" && analysis/,
    "the callout must be gated on the analyze result it reports");
  assert.match(onboarding, /scanSummary\(\{[\s\S]*?transcripts: analysis\.transcripts/);
  assert.match(onboarding, /skills: analysis\.skills/,
    "skills must be the number QUEUED (analyze), not the number found (extract)");
  assert.match(onboarding, /proposals: proposals\.length/);
  assert.doesNotMatch(onboarding, /Scanned <strong>\{history\.transcripts\}/,
    "the old two-response sentence must not come back");
});

// ── the unavailable headline states the condition actually detected ────────
//
// `unavailable` is reached on exactly one condition: the extract found no
// transcripts AND no skills. It used to announce that a particular third-party
// IDE was not running, with the real reason demoted to grey parenthetical text —
// so a developer who uses Claude Code and simply has no history on this machine
// was told a product they have never heard of was missing.
test("the empty-history headline names the missing history, not a missing IDE", () => {
  const callout = onboarding.match(/scanPhase === "unavailable"[\s\S]*?<\/div>\n/)?.[0];
  assert.ok(callout, "could not locate the unavailable callout");
  assert.match(callout, /No past AI conversations or skills found on this machine/);
  assert.doesNotMatch(callout, /No running .*IDE detected/,
    "the headline must not claim a detection it did not make");
  // The server's own detail still rides along — it names the sources it looked in.
  assert.match(callout, /\{history\.detail\}/);
});

// ── the AI-history step names no product but Claude Code ───────────────────
//
// The two placeholders checked below were publication-shape scrub leftovers
// from a redaction pass — not real products, and meaningless to a user.
test("the AI-history step names no product but Claude Code", () => {
  assert.doesNotMatch(onboarding, /\bide-agent\b/,
    "the scrub placeholder must never be shown to a user");
  assert.doesNotMatch(onboarding, /\bagent-a\b/,
    "the scrub placeholder must never be shown to a user");
  assert.match(onboarding, /reads your past <strong>Claude Code<\/strong> conversations/);
  assert.match(onboarding, /Booting up — reading your Claude Code conversations…/);
});
