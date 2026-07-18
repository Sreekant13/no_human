import test from "node:test";
import assert from "node:assert/strict";
import { makeEndpointGate } from "./queueHealthGate.js";

test("404 marks the endpoint unsupported and stops fetching", async () => {
  let calls = 0;
  const gated = makeEndpointGate(async () => { calls += 1; return { ok: false, status: 404 }; });
  assert.equal(await gated(), null);
  assert.equal(await gated(), null);
  assert.equal(calls, 1); // never re-fetches an endpoint the server lacks
});

test("non-404 errors still throw and keep polling", async () => {
  let calls = 0;
  const gated = makeEndpointGate(async () => { calls += 1; return { ok: false, status: 500 }; });
  await assert.rejects(() => gated());
  await assert.rejects(() => gated());
  assert.equal(calls, 2); // transient server errors are retried next poll
});

test("success passes through", async () => {
  const gated = makeEndpointGate(async () => ({ ok: true, status: 200, json: async () => ({ stuck: false }) }));
  assert.deepEqual(await gated(), { stuck: false });
});
