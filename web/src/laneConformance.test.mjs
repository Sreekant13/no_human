// Conformance: the JS lane routing must agree, case for case, with the Python
// source of truth in src/no_human/core/lanes.py.
//
// Both runners load the SAME file - testdata/lane_conformance.json - so the two
// implementations cannot drift apart silently. If you add a case here, the
// Python side (tests/test_lane_conformance.py) picks it up on the next run
// without anyone editing it.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { LANES, routeTask, computeLane, isWaiting } from "./boardLanes.js";

const FIXTURE_URL = new URL("../../testdata/lane_conformance.json", import.meta.url);
const { cases } = JSON.parse(readFileSync(FIXTURE_URL, "utf8"));

test("the shared fixture file is non-empty (a missing file must not pass vacuously)", () => {
  assert.ok(Array.isArray(cases));
  assert.ok(cases.length >= 20, `only ${cases?.length} cases loaded`);
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

test("a null/undefined task still routes without throwing", () => {
  assert.equal(routeTask(null), "working");
  assert.equal(routeTask(undefined), "working");
  assert.equal(computeLane(null), "working");
  assert.equal(isWaiting(null), false);
});
