import test from "node:test";
import assert from "node:assert/strict";
import { shouldTriggerNewTask } from "./keyboardShortcut.js";

test("'n' with no modifiers, non-editable target, modal closed triggers", () => {
  const result = shouldTriggerNewTask(
    { key: "n", target: { tagName: "DIV" } },
    { modalOpen: false }
  );
  assert.equal(result, true);
});

test("'n' while typing in an input does not trigger", () => {
  const result = shouldTriggerNewTask(
    { key: "n", target: { tagName: "INPUT" } },
    { modalOpen: false }
  );
  assert.equal(result, false);
});

test("'n' while target is contentEditable does not trigger", () => {
  const result = shouldTriggerNewTask(
    { key: "n", target: { tagName: "DIV", isContentEditable: true } },
    { modalOpen: false }
  );
  assert.equal(result, false);
});

test("ctrl+n does not trigger", () => {
  const result = shouldTriggerNewTask(
    { key: "n", target: { tagName: "DIV" }, ctrlKey: true },
    { modalOpen: false }
  );
  assert.equal(result, false);
});

test("meta+n does not trigger", () => {
  const result = shouldTriggerNewTask(
    { key: "n", target: { tagName: "DIV" }, metaKey: true },
    { modalOpen: false }
  );
  assert.equal(result, false);
});

test("'n' while the modal is already open does not trigger", () => {
  const result = shouldTriggerNewTask(
    { key: "n", target: { tagName: "DIV" } },
    { modalOpen: true }
  );
  assert.equal(result, false);
});

test("uppercase 'N' does not trigger", () => {
  const result = shouldTriggerNewTask(
    { key: "N", target: { tagName: "DIV" } },
    { modalOpen: false }
  );
  assert.equal(result, false);
});
