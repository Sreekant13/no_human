// C3-G4: pure helpers for the cross-task session search results. Kept separate
// from the React component so they are unit-testable without a DOM.

// The event kinds indexed in events_fts (migration 0006), mapped to short human
// labels. An unknown kind falls back to itself so the UI never shows blank.
const KIND_LABELS = {
  attempt_failed: "failed attempt",
  review: "review",
  blocked: "blocked",
  tamper: "tamper guard",
  pr_ci_red: "CI red",
  escalated: "escalated",
  ci_gate_fail: "CI_GATE fail",
};

export function kindLabel(kind) {
  return KIND_LABELS[kind] || (kind || "event");
}

// Group flat search hits by task, preserving rank order (first occurrence of a
// task wins its position). Returns [{ task_id, task_title, hits: [...] }].
export function groupByTask(results) {
  const order = [];
  const byId = new Map();
  for (const r of results || []) {
    if (!byId.has(r.task_id)) {
      const group = { task_id: r.task_id, task_title: r.task_title, hits: [] };
      byId.set(r.task_id, group);
      order.push(group);
    }
    byId.get(r.task_id).hits.push(r);
  }
  return order;
}
