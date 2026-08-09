import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import * as api from "./api.js";

// Guards the in-app path to PROVING a repo's test command.
//
// The defect these cover: the wizard derived a profile, printed "unproven", and
// stopped. The only way onward was `nh onboard <repo> --confirm` — a CLI command
// a GUI user has no way to discover — and the summary step said "Ready." anyway.
// A repo in that state still runs tasks; what it lacks is a test command for the
// review gate to run, which is most of what makes a result worth trusting.

const SRC = dirname(fileURLToPath(import.meta.url));
const onboarding = readFileSync(join(SRC, "Onboarding.jsx"), "utf8");
const app = readFileSync(join(SRC, "App.jsx"), "utf8");
const css = readFileSync(join(SRC, "styles.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

// ── the client can actually reach the new endpoints ────────────────────────

test("api.js exposes the prove/confirm/readiness surface", () => {
  assert.equal(typeof api.proveRepoSSE, "function");
  assert.equal(typeof api.confirmRepoProfile, "function");
  assert.equal(typeof api.fetchReadiness, "function");
});

test("proveRepoSSE POSTs to the prove endpoint and returns an abortable handle", async () => {
  const calls = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, opts) => {
    calls.push({ url, opts });
    return { ok: true, body: { getReader: () => ({ read: async () => ({ done: true }) }) } };
  };
  try {
    const handle = api.proveRepoSSE(
      { repo_path: "/r", test_cmd: "make test" }, () => {}, () => {});
    assert.equal(typeof handle.close, "function");
    await new Promise((r) => setTimeout(r, 5));
  } finally {
    globalThis.fetch = realFetch;
  }
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /\/api\/onboarding\/repos\/prove$/);
  assert.equal(calls[0].opts.method, "POST");
  const body = JSON.parse(calls[0].opts.body);
  // The command must travel verbatim — the UI never "helps" by editing it.
  assert.equal(body.test_cmd, "make test");
  assert.equal(body.repo_path, "/r");
});

test("proveRepoSSE surfaces a server error frame instead of silently ending", async () => {
  const realFetch = globalThis.fetch;
  const chunks = [
    'data: {"kind": "error", "text": "boom"}\n\n',
  ].map((s) => new TextEncoder().encode(s));
  let i = 0;
  globalThis.fetch = async () => ({
    ok: true,
    body: { getReader: () => ({ read: async () => (i < chunks.length ? { done: false, value: chunks[i++] } : { done: true }) }) },
  });
  let seen = null;
  try {
    api.proveRepoSSE({ repo_path: "/r" }, () => {}, (e) => { seen = e; });
    await new Promise((r) => setTimeout(r, 10));
  } finally {
    globalThis.fetch = realFetch;
  }
  assert.ok(seen, "an error frame must reach onError");
  assert.match(seen.message, /boom/);
});

// ── the wizard offers the proof rather than deferring to a CLI ──────────────

test("the repos step offers proving in-app and no longer defers to `nh onboard`", () => {
  assert.match(onboarding, /Prove test command/,
    "the repos step must offer to run the proof");
  assert.match(onboarding, /proveRepoSSE/,
    "proving must stream, not block on a silent request");
  assert.doesNotMatch(onboarding, /happens later via <code>nh onboard<\/code>/,
    "the wizard must not send a GUI user to a CLI command to finish setup");
});

test("the unproven chip is no longer a dead end", () => {
  // The old chip hardcoded the literal string "· unproven" with nothing to click.
  assert.doesNotMatch(onboarding, /`? · unproven`|\· unproven/,
    "a bare 'unproven' chip with no remedy must not come back");
  assert.match(onboarding, /ProvePanel/,
    "an onboarded repo must render the prove affordance");
});

test("a failing proof is recoverable, not a dead end", () => {
  assert.match(onboarding, /status === "failed"/);
  assert.match(onboarding, /Run this command/,
    "a failed proof must let the user correct the command and retry");
  assert.match(onboarding, /onEditCmd/,
    "the command must be editable after a failure");
});

test("proving streams real output rather than showing a bare spinner", () => {
  assert.match(onboarding, /ob-prove-log/);
  assert.match(onboarding, /f\.kind === "output"/,
    "streamed output frames must be rendered");
  assert.match(onboarding, /f\.kind === "heartbeat"/,
    "a quiet run must still report elapsed progress");
});

// ── the summary must not claim readiness it does not have ──────────────────

test("the summary headline is derived from server readiness, not hardcoded", () => {
  const summary = onboarding.match(/step\.key === "summary" &&([\s\S]*?)\n {10}\)}/)?.[1];
  assert.ok(summary, "could not locate the summary step block");
  assert.doesNotMatch(summary, /<h2 className="ob-h2">Ready\.<\/h2>/,
    '"Ready." must not be printed unconditionally');
  assert.match(summary, /readiness\.usable > 0 \? "Ready\."/,
    "the headline must depend on whether anything is actually usable");
  assert.match(summary, /Almost ready\./);
  assert.match(summary, /proven test command/,
    "the summary must report how many repos have a proven test command");
});

test("the summary explains the consequence rather than just flagging a state", () => {
  assert.match(onboarding, /review gate will have no tests to execute/,
    "the user must be told what an unproven repo actually costs them");
});

// ── the board carries no standing nag ──────────────────────────────────────
//
// A board-level banner listing every unproven repo was removed on 2026-08-05:
// it enumerated the user's local repos by name on each visit, which is both a
// nag and an unnecessary disclosure. Proving still lives where the user asked
// for it — the onboarding wizard (covered above) and `nh onboard <repo>`.

test("no standing unproven banner is mounted on the board", () => {
  assert.ok(!app.includes("UnprovenBanner"),
    "the board must not re-acquire a standing banner that lists local repos");
});

// ── onboarding ends on a first task ────────────────────────────────────────

test("onboarding ends on a first task when a repo is actually ready", () => {
  assert.match(onboarding, /Create your first task in \$\{repoName\(firstTaskRepo\)\}/);
  assert.match(onboarding, /onComplete\(firstTaskRepo \? \{ firstTaskRepo \} : \{\}\)/);
  // …and only when the SERVER says a repo is usable.
  assert.match(onboarding, /readiness\.first_usable/);
  assert.match(app, /res\.firstTaskRepo\)/);
});

// The button says "Create your first task in <repo>". The composer opened with
// an EMPTY repository field showing its placeholder, because App.jsx read the
// handover as a boolean — `if (res && res.firstTaskRepo) setShowNewTask(true)` —
// and threw the path away. The user retyped a path they had just spent minutes
// proving, at the exact instant the setup's payoff was supposed to land.
test("the proved repo reaches the composer, not just the decision to open it", () => {
  assert.match(app, /setNewTaskSeed\(\{ repoPath: res\.firstTaskRepo \}\)/,
    "the path itself must be kept, not used as a boolean and discarded");
  assert.match(app, /<NewTaskModal\s+initial=\{newTaskSeed\}/,
    "the first-task modal must be seeded with it");
  assert.match(app, /setShowNewTask\(false\); setNewTaskSeed\(null\)/,
    "closing must clear the seed, or the next '+ New Task' opens pre-filled");
  // Not the Backlog path's remount: that key belongs to the ticket queue, and
  // sharing it would couple two unrelated seeds.
  assert.doesNotMatch(app, /<NewTaskModal\s+initial=\{newTaskSeed\}\s+key=/);
  // The seed is only useful because the composer already reads it.
  const composer = readFileSync(join(SRC, "TaskComposer.jsx"), "utf8");
  assert.match(composer, /useState\(initial\?\.repoPath \?\? ""\)/,
    "TaskComposer must still seed repoPath from `initial`");
});

// ── a running prove has a way out ──────────────────────────────────────────
//
// While `status === "running"` the panel rendered a spinner and elapsed seconds
// and nothing else. On a real monorepo that is a 5-20 minute wait with no
// control: the only escape was reloading the page, which discards the entire
// wizard (team, selections, docs and project definitions are React state and
// none of it is persisted).
test("a running prove can be stopped without reloading the wizard", () => {
  assert.match(onboarding, /function stopProve\(path\)/);
  assert.match(onboarding, /onStop=\{\(\) => stopProve\(r\.path\)\}/,
    "the panel must be wired to the stop handler");
  const panel = onboarding.slice(onboarding.indexOf("export function ProvePanel"));
  assert.match(panel, /status === "running" &&[\s\S]{0,600}?onClick=\{onStop\}/,
    "Stop must be offered while the run is in flight, not only afterwards");
  // Closing the stream is what aborts it — the same close() the unmount cleanup
  // uses — and the panel must come back to a state the user can act in.
  assert.match(onboarding, /const s = proveStreams\.current\[path\];[\s\S]{0,200}?s\.close\(\)/);
  assert.match(onboarding, /status: "idle"/,
    "stopping must re-enable the Prove/Retry control, not leave a dead panel");
});

// ── styling exists for every class the new UI renders ──────────────────────

test("the prove UI has styles in both themes", () => {
  for (const cls of [".ob-prove", ".ob-prove-log", ".ob-prove-verdict"]) {
    assert.ok(css.includes(cls), `${cls} is rendered but never styled`);
  }
});
