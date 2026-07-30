import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// Like pathInput.test.mjs, there is no React renderer in this harness, so
// these assertions pin WIRING only — that a successful create-repo moves
// focus to the repository PathInput via a ref (never the unmounting clicked
// button) and announces through the existing role="alert" element. The
// BEHAVIOUR (actual focus landing, the alert becoming visible) is measured in
// a real browser by e2e/composer.mjs.

const here = fileURLToPath(new URL(".", import.meta.url));
const read = (f) => readFileSync(here + f, "utf8");

test("the repository PathInput carries the focus ref", () => {
  const src = read("TaskComposer.jsx");
  assert.match(src, /<PathInput\s+ref=\{repoInputRef\}[\s\S]{0,400}aria-label="Repository path"/);
});

test("the success-transition focus effect is keyed to [repoCreated] only, not the clicked button", () => {
  const src = read("TaskComposer.jsx");
  const effectMatch = src.match(
    /useEffect\(\(\) => \{[\s\S]*?repoInputRef\.current\?\.focus\(\);[\s\S]*?\}, \[repoCreated\]\);/
  );
  assert.ok(effectMatch, "expected a useEffect that calls repoInputRef.current?.focus() with dep array [repoCreated]");
  // The clicked "Create repo" button unmounts along with the whole panel on
  // success (Board.jsx closeTask lesson: capture the target up front, never
  // read it off the unmounting trigger) — the handler must never focus via
  // the event target/currentTarget.
  const handlerMatch = src.match(/async function handleCreateRepo\(\)\s*\{[\s\S]*?\n  \}/);
  assert.ok(handlerMatch, "expected to find handleCreateRepo's body");
  assert.doesNotMatch(handlerMatch[0], /e\.currentTarget\.focus\(\)|e\.target\.focus\(\)/);
});

test("a successful create arms the one-shot flag via setRepoCreated(true) in handleCreateRepo", () => {
  const src = read("TaskComposer.jsx");
  const handlerMatch = src.match(/async function handleCreateRepo\(\)\s*\{[\s\S]*?\n  \}/);
  assert.ok(handlerMatch, "expected to find handleCreateRepo's body");
  assert.match(handlerMatch[0], /setRepoCreated\(true\)/);
});

test("success is announced via the existing role=\"alert\" element pattern", () => {
  const src = read("TaskComposer.jsx");
  assert.match(src, /role="alert"[\s\S]{0,200}Repository created/);
});
