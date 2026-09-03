import test from "node:test";
import assert from "node:assert/strict";
import { approvedAtOf, supersededAtOf, approvalLive } from "./approvalState.js";

// The bug this ticket fixes: 16 rows carried approved_at while sitting in a
// status other than awaiting_approval (failed/escalated/implementing), and
// every surface that read a bare `task.approved_at` kept saying "approved -
// merge pending" on a task that had already moved into Needs Answer.
// approvalLive is the ONE predicate the board (cardFacts.js), the drawer
// (slideOverSummary.js's taskApprovedAt) and the needs-you count
// (boardLanes.js's isNeedsYou) all derive from, so none of them can
// disagree with the lane a task is actually sitting in again.
//
// Both payload shapes matter: TaskSummaryOut (the board's GET /api/tasks
// list) hoists approved_at/approval_superseded_at to the top level; TaskOut
// (the drawer's per-task fetch) only carries them nested under `context`.

test("approvedAtOf reads the flat field", () => {
  assert.equal(approvedAtOf({ approved_at: "2026-08-01T00:00:00Z" }), "2026-08-01T00:00:00Z");
});

test("approvedAtOf reads the nested context field when the flat one is absent", () => {
  assert.equal(
    approvedAtOf({ context: { approved_at: "2026-08-01T00:00:00Z" } }),
    "2026-08-01T00:00:00Z",
  );
});

test("approvedAtOf is null when neither shape carries it", () => {
  assert.equal(approvedAtOf({}), null);
  assert.equal(approvedAtOf(null), null);
  assert.equal(approvedAtOf({ context: {} }), null);
});

test("supersededAtOf reads the flat field, then the nested one, then null", () => {
  assert.equal(supersededAtOf({ approval_superseded_at: "2026-08-02T00:00:00Z" }), "2026-08-02T00:00:00Z");
  assert.equal(
    supersededAtOf({ context: { approval_superseded_at: "2026-08-02T00:00:00Z" } }),
    "2026-08-02T00:00:00Z",
  );
  assert.equal(supersededAtOf({}), null);
});

test("approvalLive: true only for an unsuperseded approval still sitting in awaiting_approval", () => {
  assert.equal(
    approvalLive({ status: "awaiting_approval", approved_at: "2026-08-01T00:00:00Z" }),
    true,
  );
  assert.equal(
    approvalLive({
      status: "awaiting_approval",
      context: { approved_at: "2026-08-01T00:00:00Z" },
    }),
    true,
    "the nested TaskOut payload shape must also read live",
  );
});

test("approvalLive: false with no approved_at at all, regardless of status", () => {
  assert.equal(approvalLive({ status: "awaiting_approval" }), false);
  assert.equal(approvalLive({}), false);
  assert.equal(approvalLive(null), false);
});

// The 16-row contradiction, exactly: approved_at present, status has moved
// on, and (once the fix lands) approval_superseded_at stamped too.
for (const status of ["failed", "escalated", "implementing", "paused_quota", "done"]) {
  test(`approvalLive: false once superseded, status=${status}`, () => {
    assert.equal(
      approvalLive({
        status,
        approved_at: "2026-08-01T00:00:00Z",
        approval_superseded_at: "2026-08-02T00:00:00Z",
      }),
      false,
    );
  });
}

// Belt-and-suspenders: even a LEGACY row that predates the marker (no
// approval_superseded_at stamped, e.g. from before this fix shipped, or a
// server that hasn't backfilled it) must not read live once the status has
// simply moved off awaiting_approval — approvalLive re-checks status
// directly rather than trusting the marker alone. This is what makes the
// existing 16 contradictory rows stop showing the chip with NO data
// migration: the render condition alone closes the gap.
for (const status of ["failed", "escalated", "implementing", "paused_quota"]) {
  test(`approvalLive: false for a legacy unmarked row, status=${status} (no migration needed)`, () => {
    assert.equal(
      approvalLive({ status, approved_at: "2026-08-01T00:00:00Z" }),
      false,
    );
  });
}

// `done` is the one legitimate off-ramp that must NOT supersede — a
// completed merge is the approval's success, not its supersession
// (core/db.py::_write_status only stamps the marker leaving awaiting_
// approval for anything OTHER than done). approvalLive is still false here
// because status is no longer awaiting_approval — the chip's job (a live
// merge-pending state) is done, not because the approval was invalidated.
test("approvalLive: false on done even without a supersede marker (status alone gates it)", () => {
  assert.equal(
    approvalLive({ status: "done", approved_at: "2026-08-01T00:00:00Z" }),
    false,
  );
});

test("approvalLive: a fresh re-approval after a cleared marker reads live again", () => {
  assert.equal(
    approvalLive({
      status: "awaiting_approval",
      approved_at: "2026-09-01T00:00:00Z",
      approval_superseded_at: null,
    }),
    true,
  );
});
