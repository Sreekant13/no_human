// Which agent role produced an event, and which role spawned a subagent.
//
// Split out of SlideOver so it can be exercised directly (`node --test`): the
// System diagram is only as honest as this mapping, and it silently lied for a
// long time — every Agent-SDK event was stamped source:"agent", so three Opus
// planner proposers and their investigation subagents rendered as work by a
// Sonnet coder session that had not started yet.

// The orchestrator's event `kind` is already role-specific (the supervisor hook
// emits `supervisor`, the reviewer emits review_*), so we derive the role from
// the kind for events that carry no `source`. A `source` field always wins.
export const SOURCE_BY_KIND = {
  supervisor: "supervisor",
  review_start: "reviewer", review: "reviewer", review_error: "reviewer",
  tamper: "reviewer",
  tool_use: "agent", agent_text: "agent",
  subagent_start: "agent", subagent_progress: "agent", subagent_done: "agent",
};

export const ROLE_LABEL = {
  worker: "Orchestrator", planner: "Planner", supervisor: "Supervisor",
  reviewer: "Reviewer", agent: "Coder",
};

// Kinds the Orchestrator emits *on behalf of another role*. It calls self.emit()
// for the supervisor's decisions and some review outcomes, so those events carry
// source:"orchestrator" and used to be credited to the Orchestrator — which is
// why the Supervisor node never appeared at all despite 18 events of its own.
const ORCHESTRATOR_EMITS_FOR = {
  supervisor: "supervisor", supervisor_decision: "supervisor",
  review_start: "reviewer", review: "reviewer", review_error: "reviewer",
  tamper: "reviewer",
};

export function eventSource(e) {
  const src = e.source || SOURCE_BY_KIND[e.kind] || "worker";
  // Backend emits source:"orchestrator" but the Orchestrator node id is "worker".
  if (src === "orchestrator") return ORCHESTRATOR_EMITS_FOR[e.kind] || "worker";
  // MoA proposers are stamped `planner:<lens>` and the synthesis step
  // `aggregator`. They are all the planning phase, so they share one node; the
  // raw source stays on the event, and the per-lens split shows up as separate
  // subagent nodes underneath.
  if (src === "aggregator" || src.startsWith("planner")) return "planner";
  return src;
}

// The lens a planner event came from ("test-first"), or null for every other role.
export function eventLens(e) {
  const src = e.source || "";
  return src.startsWith("planner:") ? src.slice("planner:".length) : null;
}

// The orchestrator emits one `models` event per attempt naming the model bound
// to each role. Map those role names onto the diagram's node ids so each node
// can say which model ran it — the check that would have caught a frozen
// config.yaml inverting coder and reviewer.
const MODEL_ROLE_TO_NODE = {
  coder: "agent", planner: "planner", reviewer: "reviewer", supervisor: "supervisor",
};

export function modelsByNode(events) {
  const out = {};
  for (const e of events) {
    if (e.kind !== "models" || !e.models) continue;
    for (const [role, model] of Object.entries(e.models)) {
      const node = MODEL_ROLE_TO_NODE[role];
      if (node && model) out[node] = model;   // a later attempt wins
    }
  }
  return out;
}

// Discover subagents from events at render time. Each one is attributed to the
// role that spawned it — a planner proposer's investigation subagent is not the
// Coder's work, and rendering it under Coder is how the diagram used to lie.
// The SDK reports a backgrounded shell command with the same subagent_start /
// subagent_done events it uses for a real Agent-tool dispatch, distinguished
// only by task_type. Drawing `local_bash` as a node put fourteen bash
// one-liners on the diagram as though the Coder had spawned fourteen agents.
const NOT_A_SUBAGENT = new Set(["local_bash"]);

export function discoverSubagents(events) {
  const subs = new Map(); // task_id → { id, label, type, desc, status, parent }
  for (const e of events) {
    if (e.kind === "subagent_start") {
      const tid = e.task_id;
      if (NOT_A_SUBAGENT.has(e.task_type)) continue;
      if (tid && !subs.has(tid)) {
        const parent = eventSource(e);
        const lens = eventLens(e);
        subs.set(tid, {
          id: `sub-${tid}`,
          label: (e.text || "Subagent").slice(0, 40),
          type: (e.task_type || "SUBAGENT").toUpperCase(),
          icon: "◇",
          desc: lens
            ? `${e.text || "Subagent"} — spawned by the ${lens} proposer`
            : `${e.text || "Subagent"} — spawned by ${ROLE_LABEL[parent] || parent}`,
          color: `var(--agent-${parent})`,
          subagentTaskId: tid,
          status: "active",
          parent,
          lens,
        });
      }
    } else if (e.kind === "subagent_done") {
      const tid = e.task_id;
      const sub = subs.get(tid);
      if (sub) sub.status = e.status === "completed" ? "done" : "error";
    }
  }
  return [...subs.values()];
}
