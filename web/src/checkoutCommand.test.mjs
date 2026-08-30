import test from "node:test";
import assert from "node:assert/strict";
import { checkoutCommand } from "./checkoutCommand.js";

test("builds a fetch+switch line", () => {
  assert.equal(checkoutCommand("nh/task-abc"), "git fetch origin nh/task-abc && git switch nh/task-abc");
  assert.equal(checkoutCommand(""), "");
});

test("an explicit remote overrides the origin default", () => {
  assert.equal(
    checkoutCommand("nh/task-abc", "upstream"),
    "git fetch upstream nh/task-abc && git switch nh/task-abc",
  );
});
