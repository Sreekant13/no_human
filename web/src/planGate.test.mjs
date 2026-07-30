import test from "node:test";
import assert from "node:assert/strict";
import { planApproveIndex } from "./planGate.js";

const gated = {
  status: "awaiting_input",
  blocker: {
    question: "Approve this plan before any implementation token is spent?",
    options: [
      { label: "Approve the plan - start implementing", action: { approve_plan: true } },
    ],
  },
};

test("finds the 1-based index of the plan-approval option", () => {
  assert.equal(planApproveIndex(gated), 1);
});

test("finds it behind other options", () => {
  assert.equal(planApproveIndex({
    blocker: {
      options: [
        "some label",
        { label: "raise the limit", action: { set_task_config: { attempt_tokens: 1 } } },
        { label: "approve", action: { approve_plan: true } },
      ],
    },
  }), 3);
});

test("null when no option approves a plan — the Approve button keeps its free-text reply", () => {
  assert.equal(planApproveIndex({ blocker: { options: [{ label: "x", action: null }] } }), null);
  assert.equal(planApproveIndex({ blocker: { options: [] } }), null);
  assert.equal(planApproveIndex({ blocker: null }), null);
  assert.equal(planApproveIndex({}), null);
  assert.equal(planApproveIndex(null), null);
});

test("a falsy approve_plan is not an approval", () => {
  assert.equal(planApproveIndex({
    blocker: { options: [{ label: "x", action: { approve_plan: false } }] },
  }), null);
});

test("survives junk options across the JSON boundary", () => {
  assert.equal(planApproveIndex({ blocker: { options: "not-a-list" } }), null);
  assert.equal(planApproveIndex({ blocker: { options: [null, 7, { label: "a" }] } }), null);
});
