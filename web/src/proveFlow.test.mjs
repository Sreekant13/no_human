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
const banner = readFileSync(join(SRC, "UnprovenBanner.jsx"), "utf8");
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

// ── the board gives every unproven repo a clickable remedy ─────────────────

test("the board banner names the repo and offers to prove it", () => {
  assert.match(banner, /needs its test command proven/);
  assert.match(banner, /Prove now/);
  assert.match(banner, /proveRepoSSE/);
  assert.match(banner, /confirmRepoProfile/);
  assert.match(banner, /fetchReadiness/);
});

test("the banner is mounted on the board", () => {
  assert.match(app, /import UnprovenBanner from "\.\/UnprovenBanner\.jsx"/);
  assert.match(app, /page === "board" && <UnprovenBanner \/>/);
});

test("the banner hides itself when nothing needs proving", () => {
  assert.match(banner, /if \(pending\.length === 0\) return null/,
    "a satisfied banner must disappear, not nag");
});

// ── onboarding ends on a first task ────────────────────────────────────────

test("onboarding ends on a first task when a repo is actually ready", () => {
  assert.match(onboarding, /Create your first task in \$\{repoName\(firstTaskRepo\)\}/);
  assert.match(onboarding, /onComplete\(firstTaskRepo \? \{ firstTaskRepo \} : \{\}\)/);
  // …and only when the SERVER says a repo is usable.
  assert.match(onboarding, /readiness\.first_usable/);
  assert.match(app, /res\.firstTaskRepo\) setShowNewTask\(true\)/);
});

// ── styling exists for every class the new UI renders ──────────────────────

test("the prove UI has styles in both themes", () => {
  for (const cls of [".ob-prove", ".ob-prove-log", ".ob-prove-verdict",
                     ".unproven-banner", ".unproven-log", ".btn-prove"]) {
    assert.ok(css.includes(cls), `${cls} is rendered but never styled`);
  }
});
