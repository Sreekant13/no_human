import test from "node:test";
import assert from "node:assert/strict";
import { newlyNeedsYou, notificationBody, titleWithBadge } from "./notifications.js";

const NEEDY = new Set(["awaiting_approval", "awaiting_input", "escalated"]);

test("title badge carries the needs-you count", () => {
  assert.equal(titleWithBadge(0), "no_human");
  assert.equal(titleWithBadge(3), "(3) no_human");
});

test("only NEW arrivals notify — reload and reconnect never re-fire", () => {
  const before = [
    { id: "a", status: "awaiting_approval" },
    { id: "b", status: "implementing" },
  ];
  const after = [
    { id: "a", status: "awaiting_approval" },   // already needy — silent
    { id: "b", status: "escalated" },            // NEW arrival — notify
    { id: "c", status: "implementing" },         // not needy — silent
  ];
  const fresh = newlyNeedsYou(before, after, NEEDY);
  assert.deepEqual(fresh.map((t) => t.id), ["b"]);
  // First snapshot (empty prev = page load): everything needy is "new",
  // which is why App only notifies when the tab is hidden.
  assert.equal(newlyNeedsYou([], after, NEEDY).length, 2);
});

test("a task leaving and re-entering needs-you fires again", () => {
  const parked = [{ id: "a", status: "awaiting_input" }];
  const working = [{ id: "a", status: "implementing" }];
  assert.equal(newlyNeedsYou(parked, working, NEEDY).length, 0);
  assert.equal(newlyNeedsYou(working, parked, NEEDY).length, 1);
});

test("notification body is compact and informative", () => {
  const body = notificationBody({
    id: "84251cb2d766404c", status: "awaiting_approval",
    title: "Per-PR CI_GATE Integration Test Pipeline",
  });
  assert.ok(body.startsWith("84251cb2 · awaiting approval — Per-PR"));
  assert.ok(body.length <= 120);
});
