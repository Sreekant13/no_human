// The mutating calls in api.js, exercised for real against a stubbed `fetch`.
//
// These assert on the Error a CALLER receives, not on the text of api.js. That
// distinction is the whole point: a regex-over-source guard ("does the file
// mention detailMessage?") passes while the call is wired to the wrong body,
// and this repo has already paid nine review rounds for exactly that pattern.
// Here the module is imported, its real function is called, and the thrown
// message is read — the same string that lands in the drawer's flash banner.
//
// Guarded defect: five mutating POSTs threw a BARE status code and discarded
// the server's `detail`. `sendBack` was the worst of them — api/app.py's
// send_back answers 409 with "task is already done" / "task is cancelled",
// the only explanation of why the operator's click did nothing, and the UI
// showed "POST send-back → 409".
import test from "node:test";
import assert from "node:assert/strict";
import {
  uploadAttachment, finishReview, replyTask, chooseBlockerOption, sendBack,
  approveTask, createTask,
} from "./api.js";

/** Make the next fetch answer with `status` and the given JSON body. */
function stubFetch({ status = 409, body = {}, json = true } = {}) {
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    calls.push({ url: String(url), method: opts?.method });
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => {
        if (!json) throw new SyntaxError("Unexpected token < in JSON");
        return body;
      },
    };
  };
  return calls;
}

const original = globalThis.fetch;
test.after(() => { globalThis.fetch = original; });

// One row per mutating call: [name, invoke, the server's 409 reason].
const MUTATORS = [
  ["sendBack", () => sendBack("t1", "redo it"), "task is already done"],
  ["replyTask", () => replyTask("t1", "an answer"), "task is not awaiting input"],
  ["chooseBlockerOption", () => chooseBlockerOption("t1", 2), "no such option"],
  ["finishReview", () => finishReview("t1"), "task is not under review"],
  ["uploadAttachment", () => uploadAttachment("t1", { name: "x.png" }), "attachment too large"],
  // Already correct before this sweep — pinned so the group cannot regress
  // back to a bare status code one function at a time.
  ["approveTask", () => approveTask("t1"), "task is 'done', not awaiting_approval"],
  ["createTask", () => createTask({ title: "t" }), "repo_path does not exist"],
];

for (const [name, invoke, reason] of MUTATORS) {
  test(`${name} surfaces the server's reason, not just the status code`, async () => {
    stubFetch({ status: 409, body: { detail: reason } });
    const err = await invoke().then(
      () => null,
      (e) => e,
    );
    assert.ok(err instanceof Error, `${name} must reject on a non-2xx`);
    assert.ok(
      err.message.includes(reason),
      `${name} threw "${err.message}" — the operator never sees "${reason}"`,
    );
  });

  test(`${name} names the offending field on a 422, never [object Object]`, async () => {
    stubFetch({
      status: 422,
      body: { detail: [{ type: "missing", loc: ["body", "message"], msg: "Field required" }] },
    });
    const err = await invoke().then(() => null, (e) => e);
    assert.doesNotMatch(err.message, /\[object Object\]/,
      `${name} stringified the 422 list: "${err.message}"`);
    assert.match(err.message, /message/);
    assert.match(err.message, /Field required/);
  });

  test(`${name} still reports something when the body is not JSON at all`, async () => {
    // The SPA catch-all can answer HTML; `.json()` throws and the fallback —
    // which is where the status code still belongs — has to survive.
    stubFetch({ status: 502, json: false });
    const err = await invoke().then(() => null, (e) => e);
    assert.ok(err instanceof Error);
    assert.match(err.message, /502/, `no status in the fallback: "${err.message}"`);
  });
}

test("a successful mutating call resolves with the server's JSON, not an error", async () => {
  stubFetch({ status: 200, body: { ok: true, message: "Feedback stored." } });
  assert.deepEqual(await sendBack("t1", "redo"), { ok: true, message: "Feedback stored." });
});
