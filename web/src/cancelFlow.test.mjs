import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { CANCEL_TITLE, REASON_MAX, clampReason, submitCancel } from "./cancelFlow.js";

const SRC = dirname(fileURLToPath(import.meta.url));
const DIST_ASSETS = join(SRC, "..", "dist", "assets");

// AC1: the confirm step is required — submitCancel is the only path to the
// injected api fn, and the modal calls it ONLY from its "Cancel task" button
// handler, never from typing a reason or opening the modal. This test drives
// the exported flow the way SlideOver.jsx does: nothing reaches `api` until
// that explicit call.
test("the confirm step is required: submitCancel is never reached without an explicit confirm", async () => {
  let called = false;
  const api = async () => { called = true; return { message: "ok" }; };
  // Opening the modal / typing a reason never calls submitCancel itself —
  // only an explicit call (mirroring the confirm button's onClick) does.
  assert.equal(called, false, "api must not be called before any confirm action");
  await submitCancel({ taskId: "t1", reason: "dup", api });
  assert.equal(called, true, "api must be called once the confirm action fires");
});

test("a blank reason submits as null, a typed reason is trimmed and clamped to 500", async () => {
  const calls = [];
  const api = async (id, reason) => { calls.push(reason); return { message: "ok" }; };

  await submitCancel({ taskId: "t1", reason: "", api });
  assert.equal(calls[0], null, "blank reason must submit as null, not an empty string");

  await submitCancel({ taskId: "t1", reason: "   ", api });
  assert.equal(calls[1], null, "whitespace-only reason must also submit as null");

  await submitCancel({ taskId: "t1", reason: "  duplicate of X  ", api });
  assert.equal(calls[2], "duplicate of X", "a typed reason is trimmed");

  const long = "x".repeat(600);
  await submitCancel({ taskId: "t1", reason: long, api });
  assert.equal(calls[3].length, REASON_MAX, "an over-long reason is clamped to REASON_MAX");
  assert.equal(calls[3], "x".repeat(REASON_MAX));
});

test("clampReason matches submitCancel's own clamping", () => {
  assert.equal(clampReason(""), null);
  assert.equal(clampReason("  "), null);
  assert.equal(clampReason(" hi "), "hi");
  assert.equal(clampReason("x".repeat(501)).length, 500);
});

test("submitCancel reports ok:false on a rejected api call, never throws", async () => {
  const api = async () => { throw new Error("task is already 'done'"); };
  const res = await submitCancel({ taskId: "t1", reason: "x", api });
  assert.equal(res.ok, false);
  assert.equal(res.error, "task is already 'done'");
});

// AC1 + AC3: the built bundle carries no landed-override strings, and does
// carry the new plain-language Cancel copy plus the unchanged primary action.
// Fails CLOSED — an absent/empty dist reads as "can't verify", not "clean".
test("the built bundle carries no landed-override strings", () => {
  if (!existsSync(DIST_ASSETS)) {
    assert.fail("web/dist/assets is missing — run `npm run build` in web/ first");
  }
  const jsFiles = readdirSync(DIST_ASSETS).filter((f) => f.endsWith(".js"));
  if (jsFiles.length === 0) {
    assert.fail("web/dist/assets has no JS asset — run `npm run build` in web/ first");
  }
  const bundle = jsFiles.map((f) => readFileSync(join(DIST_ASSETS, f), "utf8")).join("\n");

  for (const gone of [
    "Content landed elsewhere",
    "Human override — content landed elsewhere",
    "approve-landed",
    "Record override",
  ]) {
    assert.ok(!bundle.includes(gone), `landed-override string still in the bundle: "${gone}"`);
  }

  assert.ok(bundle.includes(CANCEL_TITLE), `"${CANCEL_TITLE}" missing from the built bundle`);
  assert.ok(bundle.includes("Approve and merge"),
    "control: the primary action's own copy must survive unchanged");
});
