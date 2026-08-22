// Run with: node --test web/src/
import test from "node:test";
import assert from "node:assert/strict";

import { decisionFor, clip } from "./blockerDecision.js";

test("non-parked task -> no decision", () => {
  assert.equal(decisionFor({ status: "implementing", blocker: { category: "X" } }), null);
  assert.equal(decisionFor({ status: "done", blocker: { category: "X" } }), null);
  assert.equal(decisionFor(null), null);
});

test("parked but no blocker -> no decision", () => {
  assert.equal(decisionFor({ status: "escalated" }), null);
  assert.equal(decisionFor({ status: "blocked", blocker: null }), null);
});

test("BUDGET_EXHAUSTED with no question and no options — the live shape", () => {
  // This is the exact shape that broke the old UI: category-only, no question,
  // no options, just a root-cause dump. The decision must still be actionable.
  const d = decisionFor({
    status: "escalated",
    blocker: {
      category: "BUDGET_EXHAUSTED",
      confidence: 1.0,
      question: null,
      options: [],
      root_cause_hypothesis: "This is a large, multi-file feature that needs more room.",
      evidence: "BUDGET: spent 3,461,106 of 4,000,000 tokens",
    },
  });
  assert.equal(d.category, "BUDGET_EXHAUSTED");
  assert.equal(d.categoryLabel, "Budget exhausted");
  assert.match(d.headline, /ran out of budget/i);
  assert.equal(d.question, null);
  assert.ok(d.ask && d.ask.length > 0, "must state what's needed even with no question");
  assert.equal(d.detail, "This is a large, multi-file feature that needs more room.");
  assert.equal(d.confidence, 1.0);
  // With no options, a primary Resume CTA is offered.
  const ids = d.actions.map((a) => a.id);
  assert.deepEqual(ids, ["resume", "reply", "cancel"]);
  assert.equal(d.actions[0].variant, "primary");
  assert.match(d.actions[0].label, /fresh budget/i);
});

test("blocker WITH one-click options -> options are the decision, no bare Resume", () => {
  const d = decisionFor({
    status: "blocked",
    blocker: {
      category: "AMBIGUOUS_REQUIREMENTS",
      question: "Is this a bug fix or a feature?",
      options: [{ label: "Bug fix", action: "set_kind:bugfix" }, "Feature"],
    },
  });
  assert.equal(d.question, "Is this a bug fix or a feature?");
  assert.equal(d.options.length, 2);
  assert.equal(d.options[0].label, "Bug fix");
  assert.equal(d.options[0].action, "set_kind:bugfix");
  // When options exist they carry the resolution; we don't add a Resume that
  // would bypass them.
  const ids = d.actions.map((a) => a.id);
  assert.deepEqual(ids, ["reply", "cancel"]);
  assert.match(d.actions[0].label, /own words/i);
});

test("explicit question with no category still yields a usable decision", () => {
  const d = decisionFor({
    status: "awaiting_input",
    blocker: { question: "Which database should I target?" },
  });
  assert.equal(d.categoryLabel, "Needs a decision");
  assert.equal(d.ask, "Which database should I target?");
  assert.equal(d.question, "Which database should I target?");
  assert.deepEqual(d.actions.map((a) => a.id), ["resume", "reply", "cancel"]);
});

test("MISSING_ACCESS maps to access-specific copy", () => {
  const d = decisionFor({
    status: "escalated",
    blocker: { category: "MISSING_ACCESS", evidence: "403 from the CI token" },
  });
  assert.equal(d.categoryLabel, "Missing access");
  assert.match(d.headline, /access/i);
  assert.equal(d.detail, "403 from the CI token"); // falls back to evidence when no hypothesis
});

test("unknown category is prettified, never crashes", () => {
  const d = decisionFor({
    status: "escalated",
    blocker: { category: "SOME_NEW_THING", question: "?" },
  });
  assert.equal(d.categoryLabel, "Some new thing");
});

test("paused_quota counts as parked", () => {
  const d = decisionFor({ status: "paused_quota", blocker: { category: "BUDGET_EXHAUSTED" } });
  assert.ok(d, "paused_quota is a parked state");
});

test("QUOTA park reads as self-resolving, not a 'waiting for your decision' gate", () => {
  // The real _park_quota shape (orchestrator.py): status paused_quota, category
  // QUOTA, wake quota_refreshed, no question/options. It auto-resumes when quota
  // refreshes, so the copy must NOT be the DEFAULT "waiting for your decision".
  const d = decisionFor({
    status: "paused_quota",
    blocker: {
      category: "QUOTA",
      wake_condition: "quota_refreshed",
      root_cause_hypothesis: "quota exhausted ('personal' subscription)",
      confidence: 1.0,
    },
  });
  assert.equal(d.category, "QUOTA");
  assert.equal(d.categoryLabel, "Paused for quota");
  assert.match(d.headline, /quota refreshes/i);
  assert.doesNotMatch(d.headline, /waiting for your decision/i); // not the DEFAULT
  assert.match(d.ask, /resumes on its own/i);
  assert.equal(d.wake, "quota_refreshed");
  assert.match(d.actions[0].label, /resume now/i);
});

test("wake_condition is surfaced when present", () => {
  const d = decisionFor({
    status: "blocked",
    blocker: { category: "STUCK", wake_condition: "the 1-hour scan finishes" },
  });
  assert.equal(d.wake, "the 1-hour scan finishes");
});

test("clip trims on a word boundary and marks truncation", () => {
  assert.equal(clip(null), null);
  assert.equal(clip("short"), "short");
  const long = "word ".repeat(100).trim();
  const c = clip(long, 40);
  assert.ok(c.length <= 41, "clipped to bound");
  assert.ok(c.endsWith("…"), "marks truncation");
  assert.ok(!c.includes("wor…"), "cut on a word boundary, not mid-word");
});

test("an infra-stamped QUOTA park says the session died, never quota", () => {
  // core/orchestrator.py _park_quota stamps blocker.infra when the SDK
  // transport died before any token (2026-08-22, a 1 MB buffer line). The
  // task retries on its own; nothing about quota is true of it.
  const d = decisionFor({
    status: "paused_quota",
    blocker: {
      category: "QUOTA",
      wake_condition: "quota_refreshed",
      root_cause_hypothesis: "SDK died before the session produced any tokens",
      confidence: 1.0,
      infra: true,
    },
  });
  assert.equal(d.categoryLabel, "Paused — session died");
  assert.doesNotMatch(d.headline, /quota/i);
  assert.doesNotMatch(d.ask, /quota/i);
  assert.match(d.ask, /retries on its own/i);
  assert.equal(d.wake, "quota_refreshed");
});
