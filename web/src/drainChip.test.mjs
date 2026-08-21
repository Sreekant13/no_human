import test from "node:test";
import assert from "node:assert/strict";
import { drainChip, formatDrainEta, formatPausedUntil } from "./drainChip.js";

test("idle: 0 busy, 0 queued, no drain time", () => {
  const chip = drainChip({ workers_busy: 0, max_workers: 4, queue_depth: 0, est_drain_seconds: null });
  assert.equal(chip.text, "0/4 workers busy · 0 queued");
  assert.ok(!chip.text.includes("drain"), "idle must never claim a drain time");
  assert.equal(chip.tone, "ok");
});

test("partial load (50% capacity): some workers busy, a queue, and an ETA", () => {
  const chip = drainChip({ workers_busy: 2, max_workers: 4, queue_depth: 5, est_drain_seconds: 2400 });
  assert.equal(chip.text, "2/4 workers busy · 5 queued · ~40 min to drain");
});

test("full capacity: every worker busy", () => {
  const chip = drainChip({ workers_busy: 4, max_workers: 4, queue_depth: 8, est_drain_seconds: 5700 });
  assert.equal(chip.text, "4/4 workers busy · 8 queued · ~1.6 h to drain");
  assert.equal(chip.tone, "warn");
});

test("no-estimate: queue exists but est_drain_seconds is null", () => {
  const chip = drainChip({ workers_busy: 2, max_workers: 4, queue_depth: 3, est_drain_seconds: null });
  assert.equal(chip.text, "2/4 workers busy · 3 queued · no estimate");
  assert.ok(!/\d+\s*(min|h)/.test(chip.text), "must never fabricate a number");
});

test("server-unreachable overrides any cached counts", () => {
  const chip = drainChip({
    error: "ECONNREFUSED",
    workers_busy: 3,
    max_workers: 4,
    queue_depth: 5,
    est_drain_seconds: 2400,
  });
  assert.equal(chip.text, "server unreachable");
  assert.equal(chip.tone, "error");
});

test("quota pause overrides the normal busy/queued/ETA readout (2026-08-20 evidence)", () => {
  // Same numbers the live defect reported ("not stuck, 0 busy, 7 queued,
  // ETA 210 min") — with `paused` set, the chip must say why instead of
  // reciting the individually-true-but-misleading figures.
  const chip = drainChip({
    workers_busy: 0,
    max_workers: 4,
    queue_depth: 7,
    est_drain_seconds: 12600,
    paused: true,
    paused_reason: "quota",
    paused_until: "2026-08-20T17:20:00+00:00",
  });
  assert.ok(chip.text.startsWith("Paused — quota resets "));
  assert.ok(!chip.text.includes("workers busy"), "must not also recite the misleading busy/queued readout");
  assert.equal(chip.tone, "warn");
});

test("quota pause wins over a missing paused_until (falls back to unknown time, never crashes)", () => {
  const chip = drainChip({ workers_busy: 0, max_workers: 4, queue_depth: 7, paused: true });
  assert.equal(chip.text, "Paused — quota resets unknown time");
});

test("infra-breaker pause reports SDK/auth failures, not quota (independent review of PR #553, 2026-08-21)", () => {
  // The infra breaker (3 consecutive zero-token/auth SDK failures) arms the
  // same cooldown clock a quota park does; paused_reason distinguishes them
  // so the chip never blames a profile for an SDK/auth outage.
  const chip = drainChip({
    workers_busy: 0,
    max_workers: 4,
    queue_depth: 7,
    est_drain_seconds: 12600,
    paused: true,
    paused_reason: "infra",
    paused_until: "2026-08-20T17:20:00+00:00",
  });
  assert.ok(chip.text.startsWith("Paused — SDK/auth failures, resumes "));
  assert.ok(!chip.text.includes("quota"), "must not also claim it was quota");
  assert.equal(chip.tone, "warn");
});

test("formatPausedUntil formats an ISO timestamp as local HH:MM, and never fabricates a time", () => {
  const formatted = formatPausedUntil("2026-08-20T17:20:00+00:00");
  assert.match(formatted, /^\d{2}:\d{2}$/);
  assert.equal(formatPausedUntil(null), "unknown time");
  assert.equal(formatPausedUntil("not-a-date"), "unknown time");
});

test("formatDrainEta never fabricates a number when the estimate is missing", () => {
  assert.equal(formatDrainEta(null), "no estimate");
  assert.equal(formatDrainEta(undefined), "no estimate");
});

test("formatDrainEta rounds sub-minute up to 1, sub-hour to whole minutes, hours to 1 decimal", () => {
  assert.equal(formatDrainEta(12), "~1 min to drain");
  assert.equal(formatDrainEta(2400), "~40 min to drain");
  assert.equal(formatDrainEta(5700), "~1.6 h to drain");
});

test("tone is always one of the closed set of tokens (no CSS vars emitted)", () => {
  const cases = [
    { workers_busy: 0, max_workers: 4, queue_depth: 0, est_drain_seconds: null },
    { workers_busy: 2, max_workers: 4, queue_depth: 5, est_drain_seconds: 2400 },
    { workers_busy: 4, max_workers: 4, queue_depth: 8, est_drain_seconds: 5700 },
    { error: "ECONNREFUSED", workers_busy: 1, max_workers: 4, queue_depth: 1, est_drain_seconds: 60 },
    { workers_busy: 0, max_workers: 4, queue_depth: 7, paused: true, paused_until: "2026-08-20T17:20:00+00:00" },
  ];
  for (const c of cases) {
    assert.ok(["ok", "warn", "error"].includes(drainChip(c).tone));
  }
});
