import test from "node:test";
import assert from "node:assert/strict";
import { initialDrainReadout, nextDrainReadout, readoutPayload } from "./drainReadout.js";
import { drainChip } from "./drainChip.js";
import { makeEndpointGate } from "./queueHealthGate.js";

// Simulates App.jsx's poll loop: a mocked fetch response feeds the same
// gate + reducer wiring the real header uses, end to end to the chip.
function makePoller(responses) {
  let i = 0;
  const gated = makeEndpointGate(async () => responses[Math.min(i++, responses.length - 1)]);
  let state = initialDrainReadout;
  return {
    async poll() {
      try {
        const h = await gated();
        state = nextDrainReadout(state, h);
      } catch {
        state = nextDrainReadout(state, "error");
      }
      return readoutPayload(state);
    },
  };
}

test("renders nothing before the first successful fetch", () => {
  assert.equal(readoutPayload(initialDrainReadout), null);
});

test("a poll failure before any success stays hidden, not unreachable", async () => {
  const { poll } = makePoller([{ ok: false, status: 500 }]);
  const payload = await poll();
  assert.equal(payload, null);
});

test("first success renders the real payload, never phantom zeros", async () => {
  const body = { workers_busy: 2, max_workers: 4, queue_depth: 3, est_drain_seconds: 90 };
  const { poll } = makePoller([{ ok: true, status: 200, json: async () => body }]);
  const payload = await poll();
  assert.deepEqual(payload, body);
  assert.equal(drainChip(payload).text, "2/4 workers busy · 3 queued · ~2 min to drain");
});

test("a later failure after a prior success flips to unreachable, dropping stale numbers", async () => {
  const body = { workers_busy: 1, max_workers: 4, queue_depth: 0, est_drain_seconds: null };
  const { poll } = makePoller([
    { ok: true, status: 200, json: async () => body },
    { ok: false, status: 500 },
  ]);
  const first = await poll();
  assert.deepEqual(first, body);
  const second = await poll();
  assert.deepEqual(second, { error: true });
  assert.deepEqual(drainChip(second), { text: "server unreachable", tone: "error" });
});

test("an unsupported (404-gated) endpoint leaves the chip hidden forever", async () => {
  const { poll } = makePoller([{ ok: false, status: 404 }]);
  const first = await poll();
  const second = await poll();
  assert.equal(first, null);
  assert.equal(second, null);
});

test("nextDrainReadout is a pure reducer over (state, outcome)", () => {
  const ok = nextDrainReadout(initialDrainReadout, { workers_busy: 0, max_workers: 2, queue_depth: 0, est_drain_seconds: null });
  assert.deepEqual(ok, { status: "ok", data: { workers_busy: 0, max_workers: 2, queue_depth: 0, est_drain_seconds: null } });
  const stillPending = nextDrainReadout(initialDrainReadout, "error");
  assert.deepEqual(stillPending, initialDrainReadout);
  const errored = nextDrainReadout(ok, "error");
  assert.deepEqual(errored, { status: "error", data: null });
  const unsupportedNoOp = nextDrainReadout(ok, null);
  assert.deepEqual(unsupportedNoOp, ok);
});
