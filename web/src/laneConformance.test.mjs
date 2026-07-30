// Conformance: the JS lane routing must agree, case for case, with the Python
// source of truth in src/no_human/core/lanes.py.
//
// Both runners load the SAME file - testdata/lane_conformance.json - so the two
// implementations cannot drift apart silently. If you add a case here, the
// Python side (tests/test_lane_conformance.py) picks it up on the next run
// without anyone editing it.
//
// This file is ALSO executed by the Python suite, via subprocess, in
// tests/test_lane_conformance.py::test_the_js_implementation_agrees_on_every_shared_case.
// It has to stay runnable with no node_modules for that to work: import only
// `node:` builtins and ./boardLanes.js, never a package.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { LANES, routeTask, computeLane, isWaiting } from "./boardLanes.js";

const FIXTURE_URL = new URL("../../testdata/lane_conformance.json", import.meta.url);
const { cases } = JSON.parse(readFileSync(FIXTURE_URL, "utf8"));

// A floor, not an equality: adding cases is fine, quietly deleting them is not.
// This was `>= 20` against a 28-case fixture, which left room to delete 7 pure
// edge cases - including all three PRESERVED DEFECT pins - with both suites
// green. Kept in step with FIXTURE_FLOOR in tests/test_lane_conformance.py.
const FIXTURE_FLOOR = 28;
const PRESERVED_DEFECT_PINS = 3;

test("the shared fixture file is non-empty (a missing file must not pass vacuously)", () => {
  assert.ok(Array.isArray(cases));
  assert.ok(cases.length >= FIXTURE_FLOOR, `only ${cases?.length} cases loaded, floor is ${FIXTURE_FLOOR}`);
});

test("the preserved-defect pins are still in the fixture", () => {
  const pins = cases.filter((c) => c.name.startsWith("PRESERVED DEFECT"));
  assert.ok(
    pins.length >= PRESERVED_DEFECT_PINS,
    `only ${pins.length} preserved-defect pins left: ${pins.map((p) => p.name).join(", ")}`,
  );
});

for (const c of cases) {
  test(`computeLane: ${c.name}`, () => {
    assert.equal(computeLane(c.task), c.lane);
  });
  test(`isWaiting: ${c.name}`, () => {
    assert.equal(isWaiting(c.task), c.waiting);
  });
}

// The fixtures carry no `lane` field, so routeTask must reach the fallback for
// every one of them - which is exactly the "an older server sent no lane" path.
for (const c of cases) {
  test(`routeTask falls back to the local computation: ${c.name}`, () => {
    assert.equal(routeTask(c.task), c.lane);
  });
}

test("routeTask prefers the lane the server computed", () => {
  // A deliberately WRONG server lane: if the client silently recomputed, this
  // would read "done" and the preference would be untested.
  assert.equal(routeTask({ status: "done", lane: "answer" }), "answer");
  assert.equal(computeLane({ status: "done", lane: "answer" }), "done");
});

test("every lane key the server can send is honoured", () => {
  for (const lane of LANES) {
    assert.equal(routeTask({ status: "implementing", lane: lane.key }), lane.key);
  }
});

test("a lane field that is not a real lane key is ignored, not rendered", () => {
  for (const bogus of ["waiting", "", "WORKING", 7, true, null, {}, []]) {
    assert.equal(
      routeTask({ status: "awaiting_approval", lane: bogus }),
      "review",
      `lane=${JSON.stringify(bogus)} must fall back`,
    );
  }
});

// Stated directly as well as via the fixture table, so deleting the fixture
// cases does not silently delete the guarantee. Mirrors
// test_a_falsy_wake_condition_is_the_same_as_no_wake_condition and
// test_preserved_defect_a_human_stopped_task_does_not_leave_needs_answer.
test("a falsy wake condition is the same as no wake condition", () => {
  assert.equal(computeLane({ status: "blocked", blocker_wake_condition: null }), "answer");
  assert.equal(computeLane({ status: "blocked", blocker_wake_condition: "" }), "answer");
  assert.equal(isWaiting({ status: "blocked", blocker_wake_condition: null }), false);
  assert.equal(isWaiting({ status: "blocked", blocker_wake_condition: "" }), false);
});

test("PRESERVED DEFECT: a human-stopped task does not leave Needs Answer", () => {
  // blocker_human_stopped silences isNeedsYou but is invisible to routing, so
  // the card never moves. Changing that is a product decision; these fail the
  // day someone makes it, which is the point.
  assert.equal(computeLane({ status: "escalated", blocker_human_stopped: true }), "answer");
  assert.equal(computeLane({ status: "awaiting_input", blocker_human_stopped: true }), "answer");
  assert.equal(computeLane({ status: "blocked", blocker_human_stopped: true }), "answer");
});

test("a null/undefined task still routes without throwing", () => {
  assert.equal(routeTask(null), "working");
  assert.equal(routeTask(undefined), "working");
  assert.equal(computeLane(null), "working");
  assert.equal(isWaiting(null), false);
});
