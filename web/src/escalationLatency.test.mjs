import test from "node:test";
import assert from "node:assert/strict";
import { escalationLatencyLine } from "./escalationLatency.js";

test("pluralises at N=1 vs N=3", () => {
  assert.equal(
    escalationLatencyLine({ status: "escalated", escalation_attempts: 1, escalation_tokens: 50_000 }),
    "Attempted 1 time, 50.0k tokens",
  );
  assert.equal(
    escalationLatencyLine({ status: "escalated", escalation_attempts: 3, escalation_tokens: 4_200_000 }),
    "Attempted 3 times, 4.20M tokens",
  );
});

test("null for a non-escalated task", () => {
  assert.equal(
    escalationLatencyLine({ status: "done", escalation_attempts: 3, escalation_tokens: 4_200_000 }),
    null,
  );
  assert.equal(
    escalationLatencyLine({ status: "in_progress", escalation_attempts: 1, escalation_tokens: 1 }),
    null,
  );
});

test("null (not '0 tokens') when the fields are absent — unmeasured != zero", () => {
  assert.equal(
    escalationLatencyLine({ status: "escalated", escalation_attempts: null, escalation_tokens: null }),
    null,
  );
  assert.equal(
    escalationLatencyLine({ status: "escalated" }),
    null,
  );
});

test("null for a missing/undefined task", () => {
  assert.equal(escalationLatencyLine(null), null);
  assert.equal(escalationLatencyLine(undefined), null);
});

test("token compaction matches fmtTokens", () => {
  // fmtTokens: <1000 raw, <1e6 "x.xk", else "x.xxM".
  assert.equal(
    escalationLatencyLine({ status: "escalated", escalation_attempts: 1, escalation_tokens: 500 }),
    "Attempted 1 time, 500 tokens",
  );
  assert.equal(
    escalationLatencyLine({ status: "escalated", escalation_attempts: 2, escalation_tokens: 1_500 }),
    "Attempted 2 times, 1.5k tokens",
  );
});
