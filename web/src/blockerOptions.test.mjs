// Run with: node --test web/src/
import test from "node:test";
import assert from "node:assert/strict";

import { hasAction, normalizeOption, normalizeOptions } from "./blockerOptions.js";

test("a bare string option — old rows, and every agent-raised blocker", () => {
  assert.deepEqual(normalizeOption("split into smaller tasks"), {
    label: "split into smaller tasks",
    action: null,
  });
});

test("a structured option keeps its action", () => {
  const raw = {
    label: "raise the limit for this task",
    action: { set_task_config: { max_lines_changed: 700 } },
  };
  assert.deepEqual(normalizeOption(raw), raw);
  assert.equal(hasAction(raw), true);
  assert.equal(hasAction("split into smaller tasks"), false);
});

test("junk across the JSON boundary never yields a React child that throws", () => {
  for (const junk of [null, undefined, 42, {}]) {
    const opt = normalizeOption(junk);
    assert.equal(typeof opt.label, "string");
    assert.equal(opt.action, null);
  }
  assert.deepEqual(normalizeOptions(null), []);
  assert.deepEqual(normalizeOptions(undefined), []);
});

test("the two shapes mix in one list", () => {
  const opts = normalizeOptions([
    "split into smaller tasks",
    { label: "raise the limit", action: { set_task_config: { max_lines_changed: 700 } } },
  ]);
  assert.deepEqual(opts.map((o) => o.label), ["split into smaller tasks", "raise the limit"]);
  assert.deepEqual(opts.map((o) => Boolean(o.action)), [false, true]);
});
