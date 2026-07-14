import test from "node:test";
import assert from "node:assert/strict";
import { tasksReducer } from "./tasksReducer.js";

const A = { id: "a", status: "done" };
const B = { id: "b", status: "failed" };

test("sync adopts the server's snapshot, including REMOVALS", () => {
  // The old reducer merged by id, so a task the server no longer lists survived on the board
  // until a reload. Every WS broadcast carries the complete board list, so a merge could only add.
  const after = tasksReducer([A, B], { type: "sync", tasks: [A] });
  assert.deepEqual(after.map((t) => t.id), ["a"], "b was removed server-side and must disappear");
});

test("sync applies updates and additions", () => {
  const updated = { id: "a", status: "awaiting_input" };
  const after = tasksReducer([A], { type: "sync", tasks: [updated, B] });
  assert.deepEqual(after, [updated, B]);
});

test("set replaces; an unknown action is a no-op", () => {
  assert.deepEqual(tasksReducer([A], { type: "set", tasks: [B] }), [B]);
  const state = [A];
  assert.equal(tasksReducer(state, { type: "nope" }), state);
});
