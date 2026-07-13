import test from "node:test";
import assert from "node:assert/strict";
import { splitPrompt } from "./promptSplit.js";

// The composer has ONE prompt textarea, but the backend takes title + description
// (createTask / Task.new require a title). This is that mapping — first line is
// the title, the rest is the description.

test("first line becomes the title, the remainder the description", () => {
  assert.deepEqual(splitPrompt("Add a retry to the uploader\nIt should back off\nand cap at 3."), {
    title: "Add a retry to the uploader",
    description: "It should back off\nand cap at 3.",
  });
});

test("a single-line prompt yields a title and a null description", () => {
  // Matches today's modal, where description is optional (createTask sends null).
  assert.deepEqual(splitPrompt("Fix the flaky login test"), {
    title: "Fix the flaky login test",
    description: null,
  });
});

test("trims surrounding whitespace and blank lines between title and body", () => {
  assert.deepEqual(splitPrompt("  Ship the composer  \n\n\n  Use Tailwind.  \n\n"), {
    title: "Ship the composer",
    description: "Use Tailwind.",
  });
});

test("an empty or whitespace-only prompt yields an empty title (submit stays disabled)", () => {
  for (const empty of ["", "   ", "\n\n", null, undefined]) {
    assert.deepEqual(splitPrompt(empty), { title: "", description: null });
  }
});

test("a body that is only whitespace collapses to a null description", () => {
  assert.deepEqual(splitPrompt("Title only\n   \n  "), {
    title: "Title only",
    description: null,
  });
});
