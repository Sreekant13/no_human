// Deterministic activity digests — the "what is this agent actually doing"
// layer the raw event feed buries. Everything here is computed from events the
// board already holds; no model call, no backend round-trip, honest by
// construction. One uniform shape so a single card component renders all of it:
//   { headline, facts: [[label, value]], highlights: [string], issues: [string] }

import { eventSource } from "./eventRoles.js";

const fmtDur = (s) => {
  if (!s || s < 1) return "<1s";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m${Math.round(s % 60) ? ` ${Math.round(s % 60)}s` : ""}`;
  return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;
};
const clip = (t, n = 160) => {
  const s = (t || "").replace(/\s+/g, " ").trim();
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
};
const basename = (p) => (p || "").split("/").pop();

function span(events) {
  if (events.length < 2) return 0;
  return events[events.length - 1].ts - events[0].ts;
}

function toolFiles(events) {
  const files = new Map(); // basename → touch count
  for (const e of events) {
    if (e.kind !== "tool_use") continue;
    const inp = e.tool_input || {};
    const p = inp.file_path || inp.path || inp.notebook_path;
    if (p && ["Edit", "Write", "MultiEdit", "NotebookEdit"].includes(e.tool_name)) {
      const b = basename(p);
      files.set(b, (files.get(b) || 0) + 1);
    }
  }
  return [...files.entries()].sort((a, b) => b[1] - a[1]);
}

function toolCounts(events) {
  const counts = new Map();
  for (const e of events) {
    if (e.kind !== "tool_use") continue;
    const t = e.tool_name || "tool";
    counts.set(t, (counts.get(t) || 0) + 1);
  }
  return counts;
}

// ── per-agent digests ─────────────────────────────────────────────────────── //

function coderSummary(evs) {
  const counts = toolCounts(evs);
  const files = toolFiles(evs);
  const results = evs.filter((e) => e.kind === "result" && e.text);
  const facts = [
    ["Tool calls", String(evs.filter((e) => e.kind === "tool_use").length)],
    ["Commands run", String(counts.get("Bash") || 0)],
    ["Files read", String((counts.get("Read") || 0) + (counts.get("Glob") || 0) + (counts.get("Grep") || 0))],
    ["Files edited", files.length ? files.slice(0, 8).map(([f, n]) => (n > 1 ? `${f} ×${n}` : f)).join(", ") : "none"],
  ];
  const highlights = results.slice(-2).map((e) => clip(e.text, 220));
  return {
    headline: files.length
      ? `Implementing — ${files.length} file(s) touched over ${fmtDur(span(evs))}`
      : `Exploring the repo — no edits yet (${fmtDur(span(evs))})`,
    facts, highlights, issues: [],
  };
}

function plannerSummary(evs, allEvents) {
  const moa = allEvents.filter((e) => e.kind === "planning_moa");
  const lenses = moa
    .map((e) => /proposer '([^']+)' finished \(([^)]+)\)/.exec(e.text || ""))
    .filter(Boolean)
    .map((m) => `${m[1]} (${m[2]})`);
  const plan = allEvents.filter((e) => e.kind === "planning" && /plan generated/.test(e.text || "")).pop();
  const subs = allEvents.filter(
    (e) => e.kind === "subagent_start" && eventSource(e) === "planner"
  );
  return {
    headline: lenses.length
      ? `Fanned out ${lenses.length} proposers, synthesized one plan (${fmtDur(span(evs))})`
      : `Planned in a single pass (${fmtDur(span(evs))})`,
    facts: [
      ["Proposers", lenses.length ? lenses.join("; ") : "single planner"],
      ["Research subagents", subs.length ? subs.map((s) => clip(s.text, 40)).join("; ") : "none"],
      ["Plan", plan ? clip(plan.text, 90) : "—"],
    ],
    highlights: [], issues: [],
  };
}

function reviewerSummary(evs) {
  const rounds = evs.filter((e) => e.kind === "review");
  const verdicts = rounds.map((e) =>
    e.passed
      ? `PASS${e.advisory_count ? ` (+${e.advisory_count} advisory)` : ""}`
      : `FAIL (${e.blocking_count ?? e.failed_count ?? "?"} blocking)`
  );
  const advisories = evs.filter((e) => e.kind === "review_advisory_findings").map((e) => clip(e.text, 200));
  const lastResult = evs.filter((e) => e.kind === "result" && e.text).pop();
  return {
    headline: rounds.length
      ? `${rounds.length} review round(s): ${verdicts.join(" → ")}`
      : "No review rounds yet",
    facts: [["Verdicts", verdicts.join(" → ") || "—"]],
    highlights: lastResult ? [clip(lastResult.text, 240)] : [],
    issues: advisories,
  };
}

function supervisorSummary(evs) {
  const decisions = evs.filter((e) => e.kind === "supervisor_decision");
  const corrections = decisions.filter((e) => e.text === "correct");
  const continues = decisions.length - corrections.length;
  // `message` carries the substance; older API payloads dropped it, in which
  // case only the verdict words exist — say so rather than showing nothing.
  const messages = corrections.map((e) => e.message).filter(Boolean).map((m) => clip(m, 220));
  const issues = corrections.length && !messages.length
    ? ["Correction details not in this feed (older events) — see the DB record."]
    : [];
  return {
    headline: corrections.length
      ? `Course-corrected the coder ${corrections.length}× (approved ${continues} checkpoints)`
      : `Watching — ${continues} checkpoints approved, no corrections needed`,
    facts: [["Checks", String(decisions.length)], ["Corrections", String(corrections.length)]],
    highlights: messages.slice(-4),
    issues,
  };
}

function subagentSummary(evs, agent) {
  const progress = evs.filter((e) => e.kind === "subagent_progress" && e.text);
  const done = evs.filter((e) => e.kind === "subagent_done").pop();
  return {
    headline: `${agent.label} — ${done ? (done.status === "completed" ? "finished" : done.status) : "running"} (${fmtDur(span(evs))})`,
    facts: [["Updates", String(progress.length)]],
    highlights: [
      ...(progress.length ? [clip(progress[progress.length - 1].text, 220)] : []),
      ...(done && done.text ? [clip(done.text, 220)] : []),
    ],
    issues: [],
  };
}

// The post-PR watcher: what it is shepherding right now (CI_GATE verdicts,
// CI-fix rounds, comment injections, the merge). One glance answers "is the
// autonomous post-PR loop alive and what did it last do".
function watcherSummary(evs) {
  const ci_gatePass = evs.filter((e) => e.kind === "ci_gate_pass").length;
  const ci_gateFail = evs.filter((e) => e.kind === "ci_gate_fail").length;
  const ciRounds = evs.filter((e) => e.kind === "pr_ci_red").length;
  const feedback = evs.filter((e) => e.kind === "pr_feedback").length;
  const merged = evs.some((e) => e.kind === "merged");
  const last = evs[evs.length - 1];
  const facts = [["Events", String(evs.length)]];
  if (ci_gatePass || ci_gateFail) {
    facts.push(["CI_GATE", `${ci_gatePass} passed / ${ci_gateFail} failed`]);
  }
  if (ciRounds) facts.push(["CI fix rounds", String(ciRounds)]);
  if (feedback) facts.push(["Feedback injections", String(feedback)]);
  return {
    headline: merged
      ? "PR merged by a human — task done"
      : `shepherding the PR — ${evs.length} watcher event(s) over ${fmtDur(span(evs))}`,
    facts,
    highlights: [`Now: ${clip(last.text || last.kind, 140)}`],
    issues: ci_gateFail ? [`CI_GATE failed ${ci_gateFail} time(s)`] : [],
  };
}

export function agentSummary(allEvents, agent) {
  const evs = agent.subagentTaskId
    ? allEvents.filter(
        (e) => ["subagent_start", "subagent_progress", "subagent_done"].includes(e.kind)
          && e.task_id === agent.subagentTaskId)
    : allEvents.filter((e) => eventSource(e) === agent.id);
  if (!evs.length) return null;
  if (agent.subagentTaskId) return subagentSummary(evs, agent);
  switch (agent.id) {
    case "agent": return coderSummary(evs);
    case "planner": return plannerSummary(evs, allEvents);
    case "reviewer": return reviewerSummary(evs);
    case "supervisor": return supervisorSummary(evs);
    case "watcher": return watcherSummary(evs);
    default: {
      return {
        headline: `${evs.length} orchestration events over ${fmtDur(span(evs))}`,
        facts: [["Last", clip(evs[evs.length - 1].text || evs[evs.length - 1].kind, 90)]],
        highlights: [], issues: [],
      };
    }
  }
}

// ── whole-task digest (Activity page header) ─────────────────────────────── //

export function taskSummary(events) {
  if (!events.length) return null;
  const attempts = events.filter((e) => e.kind === "attempt_start").length;
  const reviews = events.filter((e) => e.kind === "review");
  const verdictChain = reviews.map((e) => (e.passed ? "P" : "F")).join("");
  const commits = events.filter((e) => e.kind === "commit");
  const tests = events.filter((e) => e.kind === "tests").pop();
  const budget = events.filter((e) => e.kind === "lifetime_budget").pop();
  const pr = events.filter((e) => e.kind === "pr_open").pop();
  const models = events.filter((e) => e.kind === "models").pop();
  const blockers = events.filter((e) => e.kind === "escalated");
  const lastBlocker = blockers.length
    ? /\*\*Category:\*\*\s*(\w+)/.exec(blockers[blockers.length - 1].text || "")?.[1]
    : null;
  const failReasons = events
    .filter((e) => e.kind === "attempt_failed" && e.text)
    .slice(-3)
    .map((e) => clip(e.text.replace(/^review failed:\s*/, ""), 130));
  const last = events[events.length - 1];

  const facts = [
    ["Attempts", String(attempts)],
    ["Reviews", reviews.length ? `${reviews.length} (${verdictChain})` : "none"],
    ["Commits", String(commits.length)],
  ];
  if (tests) facts.push(["Tests", clip(tests.text, 60)]);
  if (budget) facts.push(["Budget", clip(budget.text, 60)]);
  if (models) facts.push(["Models", clip(models.text, 110)]);
  if (pr) facts.push(["PR", pr.text]);
  const ci_gate = events.filter(
    (e) => e.kind === "ci_gate_pass" || e.kind === "ci_gate_fail").pop();
  if (ci_gate) {
    facts.push(["CI_GATE",
      ci_gate.kind === "ci_gate_pass" ? "integration passed" : "integration FAILED"]);
  }

  return {
    headline: pr
      ? `PR open — ${clip(pr.text, 80)}`
      : `${attempts} attempt(s), ${commits.length} commit(s), ${fmtDur(span(events))} elapsed`,
    facts,
    highlights: [`Now: ${clip(last.text || last.kind, 140)}`],
    issues: [
      ...failReasons.map((r) => `Review finding: ${r}`),
      ...(lastBlocker ? [`Last blocker: ${lastBlocker}`] : []),
    ],
  };
}
