import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { feasibilityCreateToast } from "./feasibilityToast.js";

// P3: dispatch takes ~9s and SlideOver's FeasibilityCard only renders while
// task.status === "pending", so an operator who opens the drawer after that
// almost never sees the create-time hint. `feasibilityCreateToast` is the
// seam that must read the hint straight off the CREATE RESPONSE instead.

test("a create response carrying a feasibility hint yields a toast with its message", () => {
  const created = {
    id: "abc123",
    status: "pending",
    feasibility_hint: {
      band: "risky", tier: "complex", offer: "split", done_rate_pct: 40,
      message: "Only 40% of similar tasks finished in one pass.",
    },
  };
  const toast = feasibilityCreateToast(created);
  assert.ok(toast, "must produce a toast when the response carries a hint");
  assert.match(toast.message, /40%/);
  assert.ok(toast.id.includes("abc123"), "toast id must be per-task, not shared across creates");
});

// Fail-open mirror of estimate_feasibility: nothing worth flagging renders
// nothing, not an empty/placeholder toast.
test("a create response without a hint yields no toast", () => {
  assert.equal(feasibilityCreateToast({ id: "t2" }), null);
  assert.equal(feasibilityCreateToast({ id: "t3", feasibility_hint: null }), null);
});

test("a hint object with no message string still yields no toast", () => {
  assert.equal(
    feasibilityCreateToast({ id: "t4", feasibility_hint: { band: "risky", tier: "complex" } }),
    null,
  );
});

// The create-time toast is a NEW surface, not a replacement — the drawer's
// card must still gate on live status exactly as before, for a task reopened
// well after creation.
test("SlideOver's pending-status FeasibilityCard gate is unchanged", () => {
  const src = readFileSync(new URL("./SlideOver.jsx", import.meta.url), "utf8");
  assert.match(
    src,
    /task\.status === "pending"\s*\n\s*&& \(task\.context \|\| \{\}\)\.feasibility_hint\?\.offer === "split"/,
    "the pending-status gate for the FeasibilityCard must survive untouched",
  );
});

// Wiring pin: App.jsx must actually call feasibilityCreateToast on the create
// response (not just leave the module unused) and render the result as a
// status (not alert) toast — otherwise the unit tests above pass green while
// nothing on screen ever changes.
test("App.jsx wires the create response into feasibilityCreateToast and renders a status toast", () => {
  const src = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
  assert.match(src, /import \{ feasibilityCreateToast \} from "\.\/feasibilityToast\.js";/);
  assert.equal(
    (src.match(/feasibilityCreateToast\(created\)/g) || []).length >= 2,
    true,
    "both create paths (typed task + backlog queue) must feed their response through it",
  );
  assert.match(src, /className="nh-toast nh-toast-feasibility" role="status"/);
});
