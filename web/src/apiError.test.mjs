import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { detailMessage } from "./apiError.js";

const SRC = dirname(fileURLToPath(import.meta.url));

// FastAPI's error body is NOT one shape. HTTPException sends
// `{"detail": "task is 'done', not awaiting_approval"}` — a string — but a 422
// request-validation failure sends `{"detail": [{loc, msg, type}, …]}`, a LIST.
// `new Error(detail.detail)` stringified that list to "[object Object]", so the
// one class of error that says exactly which field was wrong was the one class
// the operator could not read.

test("a string detail is surfaced verbatim", () => {
  assert.equal(
    detailMessage({ detail: "task is 'done', not awaiting_approval" }, "fallback"),
    "task is 'done', not awaiting_approval",
  );
});

test("a 422 validation list becomes readable text, never [object Object]", () => {
  const body = {
    detail: [
      { type: "string_too_short", loc: ["body", "title"], msg: "String should have at least 1 character" },
      { type: "missing", loc: ["body", "repo_path"], msg: "Field required" },
    ],
  };
  const msg = detailMessage(body, "POST /api/tasks → 422");
  assert.doesNotMatch(msg, /\[object Object\]/, `unreadable: "${msg}"`);
  assert.match(msg, /title/);
  assert.match(msg, /at least 1 character/);
  assert.match(msg, /repo_path/);
  assert.match(msg, /Field required/);
  assert.doesNotMatch(msg, /\bbody\b/, "the 'body' path segment is noise to a human");
});

test("an unrecognised detail shape still yields something readable, then the fallback", () => {
  // An object detail (some handlers send a dict) must not stringify to junk.
  const obj = detailMessage({ detail: { code: "quota", remaining: 0 } }, "fb");
  assert.doesNotMatch(obj, /\[object Object\]/, obj);
  assert.match(obj, /quota/);
  // Nothing usable → the caller's status-code fallback.
  assert.equal(detailMessage({}, "fb"), "fb");
  assert.equal(detailMessage(null, "fb"), "fb");
  assert.equal(detailMessage(undefined, "fb"), "fb");
  assert.equal(detailMessage({ detail: "" }, "fb"), "fb");
  assert.equal(detailMessage({ detail: "   " }, "fb"), "fb");
  assert.equal(detailMessage({ detail: [] }, "fb"), "fb");
  assert.equal(detailMessage({ detail: {} }, "fb"), "fb");
});

// The bug was not in one function — the same `d.detail || …` line is copied
// across every mutating call in api.js. A fix applied to `approveTask` alone
// would leave the identical defect in two dozen siblings.
test("no api.js caller still passes a raw `detail` straight to new Error", () => {
  const src = readFileSync(join(SRC, "api.js"), "utf8");
  const raw = [...src.matchAll(/^.*\b\w+\.detail\s*\|\|/gm)].map((m) => m[0].trim());
  assert.deepEqual(raw, [],
    `these still stringify a 422 list to "[object Object]":\n${raw.join("\n")}`,
  );
  assert.ok(
    (src.match(/detailMessage\(/g) || []).length > 15,
    "every error path that reads `detail` must go through detailMessage",
  );
});

// A second copy of this helper elsewhere would drift; keep one.
test("detailMessage lives in exactly one module", () => {
  const defs = readdirSync(SRC)
    .filter((f) => (f.endsWith(".js") || f.endsWith(".jsx")))
    .filter((f) => /function detailMessage\b/.test(readFileSync(join(SRC, f), "utf8")));
  assert.deepEqual(defs, ["apiError.js"]);
});
