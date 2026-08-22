// Turns a parked task's blocker record into the ONE thing a human needs on the
// landing view: what's blocking, in plain language, and what they can do about
// it right now.
//
// Why this exists: a blocker's fields are agent-shaped, not human-shaped. The
// most common parked state — BUDGET_EXHAUSTED — carries no `question` and no
// `options` at all (verified on live tasks), only a `category`, an `evidence`
// dump, and a `root_cause_hypothesis`. Rendering those raw gives the operator a
// wall of text with no stated ask. `decisionFor` maps the category to a
// headline + a plain "what's needed from you" prompt + the concrete actions
// (resume / reply / cancel, or the blocker's own one-click options), so the
// drawer can lead with the decision instead of burying it.

import { normalizeOptions } from "./blockerOptions.js";

// The statuses that mean "a human must act before this moves" — kept in sync
// with slideOverSummary.PARKED_STATUSES (paused_quota is a parked variant the
// board also surfaces).
const PARKED = new Set(["awaiting_input", "blocked", "escalated", "paused_quota"]);

// Per-category human copy. `resumeLabel` is the primary CTA when the blocker
// has no explicit options of its own; `ask` states what the human decides.
// Anything not listed falls back to DEFAULT_COPY — the map is a nicety, never
// a gate, so an unknown category still produces a usable panel.
const CATEGORY_COPY = {
  BUDGET_EXHAUSTED: {
    label: "Budget exhausted",
    headline: "This task ran out of budget for its attempt",
    ask: "Give it a fresh budget to keep going, split it into smaller tasks, or stop here.",
    resumeLabel: "Resume with a fresh budget",
  },
  MISSING_ACCESS: {
    label: "Missing access",
    headline: "This task is blocked on access it doesn't have",
    ask: "Grant the access it needs and resume, or reply telling it how to proceed.",
    resumeLabel: "Resume",
  },
  STUCK: {
    label: "Stuck",
    headline: "This task couldn't make progress on its own",
    ask: "Point it in the right direction with a reply, or stop here.",
    resumeLabel: "Resume",
  },
  AMBIGUOUS_REQUIREMENTS: {
    label: "Needs a decision",
    headline: "This task hit a question it can't answer alone",
    ask: "Answer the question below so it can keep going.",
    resumeLabel: "Resume",
  },
  // A self-resolving park (backend _park_quota, category "QUOTA",
  // wake_condition "quota_refreshed"): the subscription's quota is exhausted and
  // the task auto-resumes when it refreshes. Nothing to answer — so the copy must
  // NOT read as a "waiting for your decision" gate (which the DEFAULT would).
  QUOTA: {
    label: "Paused for quota",
    headline: "This task is paused until its subscription quota refreshes",
    ask: "It resumes on its own when quota refreshes — you don't need to answer. Resume it now to retry sooner, or stop here.",
    resumeLabel: "Resume now",
  },
};

// The same park, raised by a DEAD SESSION rather than a wall: the backend
// stamps `blocker.infra` when the Agent SDK transport died before the session
// produced a token (core/orchestrator.py _park_quota). The task sleeps and
// retries on its own; nothing about quota is true of it, so the copy must not
// say quota.
const INFRA_PARK_COPY = {
  label: "Paused — session died",
  headline: "This task's agent session died before it did any work",
  ask: "It retries on its own shortly — you don't need to answer. Resume it now to retry sooner, or stop here.",
  resumeLabel: "Resume now",
};

const DEFAULT_COPY = {
  label: "Needs a decision",
  headline: "This task is waiting for your decision",
  ask: "Answer below, reply in your own words, or stop here.",
  resumeLabel: "Resume",
};

// A readable badge for the category. Hand-written copy wins; otherwise
// prettify the raw enum (BUDGET_EXHAUSTED -> "Budget exhausted"); no category
// at all -> the neutral default label.
function categoryLabelFor(category) {
  if (!category) return DEFAULT_COPY.label;
  if (CATEGORY_COPY[category]?.label) return CATEGORY_COPY[category].label;
  const s = String(category).replace(/_/g, " ").toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// Trim a long agent field to a readable teaser; the full text still lives in
// the Details section's blocker record. Cuts on a word boundary when possible.
export function clip(text, max = 220) {
  if (!text) return null;
  const s = String(text).trim();
  if (s.length <= max) return s;
  const cut = s.slice(0, max);
  const sp = cut.lastIndexOf(" ");
  return (sp > max * 0.6 ? cut.slice(0, sp) : cut).trimEnd() + "…";
}

// Returns the decision model, or null when there's nothing for a human to
// decide (task missing, not parked, or no blocker record).
export function decisionFor(task) {
  if (!task || !PARKED.has(task.status)) return null;
  const b = task.blocker;
  if (!b || typeof b !== "object") return null;

  const category = b.category ? String(b.category) : null;
  const infraPark = category === "QUOTA" && b.infra === true;
  const copy = infraPark
    ? INFRA_PARK_COPY
    : (category && CATEGORY_COPY[category]) || DEFAULT_COPY;
  const options = normalizeOptions(b.options);
  const question = b.question ? String(b.question) : null;

  // What's shown as the prompt: an explicit question wins (it's the agent's own
  // words); otherwise the category's canned ask.
  const ask = question || copy.ask;

  // The "why", trimmed. Prefer root_cause_hypothesis (the diagnosis); fall back
  // to evidence (the raw signal) when there's no hypothesis.
  const detail = clip(b.root_cause_hypothesis) || clip(b.evidence);

  // The non-option action buttons. When the blocker carries its own one-click
  // options, THOSE are the decision — we don't also offer a bare "Resume" that
  // would bypass them. Reply (free-form) and Cancel (quiet) are always offered.
  const actions = [];
  if (options.length === 0) {
    actions.push({ id: "resume", label: copy.resumeLabel, variant: "primary" });
  }
  actions.push({
    id: "reply",
    label: options.length > 0 ? "Reply in your own words" : "Reply with guidance",
    variant: "secondary",
  });
  actions.push({ id: "cancel", label: "Stop this task", variant: "quiet" });

  return {
    category,
    categoryLabel: infraPark ? INFRA_PARK_COPY.label : categoryLabelFor(category),
    headline: copy.headline,
    ask,
    // `question` is set only when the agent asked something explicit — the panel
    // renders it as a quoted question; `ask` is always present as the prompt.
    question,
    detail,
    options,
    wake: b.wake_condition ? String(b.wake_condition) : null,
    confidence: typeof b.confidence === "number" ? b.confidence : null,
    actions,
  };
}
