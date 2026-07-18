// The System page's top layer: the pipeline's five functionalities, in the
// order the orchestrator actually drives them. Roles (eventRoles.js) say who
// emitted an event; functionalities say which stage of the SDLC that work
// belongs to. Split out of SlideOver so the grouping is testable with
// `node --test` — the pipeline strip is only as honest as this mapping.

import { eventSource } from "./eventRoles.js";

export const FUNCTIONALITIES = [
  { id: "orchestrating", label: "Orchestrating", icon: "⚙", primary: "worker",
    roles: ["worker"], color: "var(--agent-worker)",
    desc: "Drives the pipeline: intake, context, attempts, blockers" },
  { id: "planning", label: "Planning", icon: "◈", primary: "planner",
    roles: ["planner"], color: "var(--agent-planner)",
    desc: "Proposes independent plans, then synthesizes one" },
  { id: "coding", label: "Coding", icon: "⌨", primary: "agent",
    roles: ["agent", "supervisor"], color: "var(--agent-agent)",
    desc: "Implements the change under live supervision" },
  { id: "reviewing", label: "Reviewing", icon: "✓", primary: "reviewer",
    roles: ["reviewer"], color: "var(--agent-reviewer)",
    desc: "Adversarial gate: review, tests, tamper check" },
  // The post-PR watcher (blockers/wake.py): merged→done, comment injection,
  // red-CI fix loop, CI_GATE integration gate. Its events carry
  // source:"watcher" and used to mis-attribute to Orchestrating — the whole
  // autonomous post-PR ladder was invisible on the board.
  { id: "shepherding", label: "Shepherding", icon: "☂", primary: "watcher",
    roles: ["watcher"], color: "var(--agent-worker)",
    desc: "Post-PR: merge watch, feedback, CI fixes, CI_GATE gate" },
];

export const FX_OF_ROLE = {
  worker: "orchestrating", planner: "planning",
  agent: "coding", supervisor: "coding", reviewer: "reviewing",
  watcher: "shepherding",
};

// Which functionality the most recent event belongs to — the "you are here"
// marker on the pipeline strip. Null when the task isn't running.
export function currentFunctionality(events, isActive) {
  if (!isActive || !events.length) return null;
  const role = eventSource(events[events.length - 1]);
  return FX_OF_ROLE[role] || "orchestrating";
}

// One entry per functionality: which of its roles have fired, its subagents,
// aggregate status and counts, and the freshest activity line. `agentStates`
// and `subagents` are what SystemTab already derives; `models` is keyed by
// node id (modelsByNode). Status precedence: error > active > done > idle —
// a stage with a live error must never read as merely "active".
export function groupFunctionalities({ agentStates, subagents, models = {}, events = [] }) {
  // Freshest event text per functionality (agentStates only knows each role's
  // own last event, and roles within a stage interleave).
  const lastByFx = {};
  for (const e of events) {
    const fx = FX_OF_ROLE[eventSource(e)];
    if (fx && e.text) lastByFx[fx] = e.text;
  }
  return FUNCTIONALITIES.map((fx) => {
    const roles = fx.roles.filter((r) => agentStates[r] && agentStates[r].count > 0);
    const subs = subagents.filter((s) => (FX_OF_ROLE[s.parent] || "orchestrating") === fx.id);
    // Subagent events carry their spawning role's source, so they are already
    // inside that role's count — adding the sub's own count would double-count
    // them. Subs only contribute when the stage has no roles of its own (a
    // sub with an unknown parent parked under Orchestrating).
    const eventCount = roles.length
      ? roles.reduce((n, r) => n + agentStates[r].count, 0)
      : subs.reduce((n, s) => n + (agentStates[s.id]?.count || 0), 0);
    const states = [
      ...roles.map((r) => agentStates[r].status),
      ...subs.map((s) => s.status === "done" ? "done" : s.status),
    ];
    const status = states.includes("error") ? "error"
      : states.includes("active") ? "active"
      : states.includes("done") ? "done"
      : "idle";
    return {
      ...fx,
      present: roles.length + subs.length > 0,
      roles, subs,
      agentCount: roles.length + subs.length,
      eventCount,
      status,
      lastText: lastByFx[fx.id] || "",
      model: models[fx.primary] || null,
    };
  });
}


// Order a grouped stage into the SYSTEM tab's agent tree: the stage's primary
// agent as a depth-0 row, then its ADDITIONAL agents as depth-1 children — the
// coding Supervisor (a live monitor, not the coder itself) and every discovered
// subagent (planner proposers' investigators). The last depth-1 child is
// flagged so the tree connector rail knows where to stop. Kept pure and here
// (not in the JSX) so the ordering is unit-testable.
export function laneAgentRows(g) {
  const out = [];
  const roles = g.roles || [];
  if (roles.includes(g.primary)) out.push({ kind: "role", id: g.primary, depth: 0 });
  // The Supervisor rides the Coding stage but is not the Coder — nest it.
  if (g.id === "coding" && roles.includes("supervisor")) {
    out.push({ kind: "role", id: "supervisor", depth: 1 });
  }
  for (const s of g.subs || []) out.push({ kind: "sub", id: s.id, sub: s, depth: 1 });
  let lastChild = -1;
  out.forEach((r, i) => { if (r.depth === 1) lastChild = i; });
  return out.map((r, i) => ({ ...r, isLastChild: i === lastChild }));
}

// A non-running task (parked / escalated / awaiting approval / terminal) has
// nothing executing: any state still reading "active" is stale, and it must
// render as done — never as live work. Errors stay errors.
export function clampAgentState(state, taskActive) {
  if (taskActive || !state || state.status !== "active") return state;
  return { ...state, status: "done" };
}
