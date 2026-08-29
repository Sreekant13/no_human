import test from "node:test";
import assert from "node:assert/strict";
import {
  classifyApproveError, approveRefusalToast, setApproveError, dismissApproveError,
  pruneApproveErrors,
} from "./approveRefusal.js";

// Reproduces the silent-click bug (task e24cee25/PR #643): a containment
// refusal — "0936e40a3 is not an ancestor of fix/global-flags-defeat-the-
// merge-rules" — reached the board as a rejected promise and rendered
// nothing. `classifyApproveError` is the seam that must turn that rejection
// into visible text; before this module existed there was no such seam, so
// this test is RED against unfixed code (no module, no derivation — the
// string dies in the catch).
test("classifyApproveError surfaces the server refusal verbatim", () => {
  const err = Object.assign(
    new Error("0936e40a3 is not an ancestor of fix/global-flags-defeat-the-merge-rules — refusing."),
    { status: 400 },
  );
  const cls = classifyApproveError(err);
  assert.equal(cls.cause, "refused");
  assert.match(
    cls.text,
    /0936e40a3 is not an ancestor of fix\/global-flags-defeat-the-merge-rules — refusing\./,
  );
  assert.equal(cls.status, 400);
});

test("an error with no message still yields visible text", () => {
  const err = Object.assign(new Error(""), { status: 409 });
  const cls = classifyApproveError(err);
  assert.ok(cls.text.length > 0, "must never be empty — that IS the silent-click bug");
  assert.match(cls.text, /409/, "the status must still be named");
});

test("an error with no message and no status still yields visible text", () => {
  const cls = classifyApproveError(new Error(""));
  assert.ok(cls.text.length > 0);
  assert.match(cls.text, /no status/i);
});

test("a timeout is labelled Request timeout, not a server refusal", () => {
  const err = Object.assign(new Error("approve timed out"), { timeout: true });
  const cls = classifyApproveError(err);
  assert.equal(cls.cause, "timeout");
  assert.match(cls.text, /Request timeout/);
  assert.doesNotMatch(cls.text, /Approve refused/);
});

test("an AbortError (no explicit timeout flag) is still classified as a timeout", () => {
  const err = new Error("The operation was aborted");
  err.name = "AbortError";
  const cls = classifyApproveError(err);
  assert.equal(cls.cause, "timeout");
});

// Regression test for the reviewer finding on the prior attempt: approve is a
// synchronous 2-4 minute server-side land, and aborting the client fetch does
// NOT cancel that server-side work — so a timeout/abort text that asserts a
// definite outcome ("Nothing was merged") is simply false; the client cannot
// know whether the land finished. It must describe uncertainty, not a result.
test("a timeout never asserts the merge did not happen", () => {
  const err = Object.assign(new Error("aborted"), { timeout: true });
  const cls = classifyApproveError(err);
  assert.doesNotMatch(
    cls.text,
    /nothing (was|has been) merged/i,
    "a client-side abort cannot cancel the server's synchronous land — the text must not claim a definite outcome",
  );
});

test("approveRefusalToast carries the classified text and the task id", () => {
  const cls = classifyApproveError(new Error("Merge already in progress"));
  const toast = approveRefusalToast("t1", cls);
  assert.equal(toast.tone, "error");
  assert.equal(toast.taskId, "t1");
  assert.match(toast.text, /Merge already in progress/);
  assert.ok(toast.id.includes("t1"), "toast id must be per-task, not shared");
});

test("setApproveError / dismissApproveError are per-task and immutable", () => {
  const clsA = classifyApproveError(new Error("Merge already in progress"));
  const clsB = classifyApproveError(new Error("task is 'done', not awaiting_approval"));
  let map = {};
  const afterA = setApproveError(map, "a", clsA);
  assert.notEqual(afterA, map, "must return a new map, not mutate in place");
  assert.equal(map.a, undefined, "the original map must be untouched");

  const afterB = setApproveError(afterA, "b", clsB);
  assert.equal(afterB.a.text, afterA.a.text);
  assert.equal(afterB.b.text, clsB.text);

  const afterDismissA = dismissApproveError(afterB, "a");
  assert.equal(afterDismissA.a, undefined, "dismiss must remove only the named task");
  assert.equal(afterDismissA.b.text, clsB.text, "dismissing a must not touch b");
  assert.notEqual(afterDismissA, afterB, "dismiss must return a new map");
});

test("dismissApproveError on an absent task is a no-op that returns the same map", () => {
  const map = { a: classifyApproveError(new Error("x")) };
  assert.equal(dismissApproveError(map, "nope"), map);
});

test("pruneApproveErrors drops ids no longer awaiting_approval and keeps the rest", () => {
  const cls = classifyApproveError(new Error("Merge already in progress"));
  const map = { a: cls, b: cls, c: cls };
  const tasks = [
    { id: "a", status: "awaiting_approval" },
    { id: "b", status: "done" }, // approved and landed — the error is now stale
    // "c" has left the task list entirely (e.g. deleted)
  ];
  const pruned = pruneApproveErrors(map, tasks);
  assert.deepEqual(Object.keys(pruned).sort(), ["a"]);
  assert.notEqual(pruned, map);
});

test("pruneApproveErrors returns the SAME map reference when nothing changes", () => {
  const cls = classifyApproveError(new Error("x"));
  const map = { a: cls };
  const tasks = [{ id: "a", status: "awaiting_approval" }];
  assert.equal(pruneApproveErrors(map, tasks), map, "no-op prune should not force a re-render");
});
