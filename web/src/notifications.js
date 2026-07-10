// Needs-You notifications (W2.2). With five tasks running in parallel the
// operator must not have to stare at the tab to catch "Needs You" — the tab
// title always carries the count (works with zero permissions), and the
// Notification API fires on each NEW arrival when the user has granted it.
// Pure derivation lives here so `node --test` can pin it; App.jsx only wires
// the effect.

export const BASE_TITLE = "no_human";

// Tab title with the needs-you count: "(3) no_human" / "no_human".
export function titleWithBadge(count) {
  return count > 0 ? `(${count}) ${BASE_TITLE}` : BASE_TITLE;
}

// Tasks that ENTERED a needs-you status between two board snapshots — the
// set to notify about. A task already needing you (page reload, reconnect)
// never re-fires; a task leaving and re-entering (revise → park again) does.
export function newlyNeedsYou(prevTasks, tasks, needsYouStatuses) {
  const wasNeedy = new Set(
    (prevTasks || [])
      .filter((t) => needsYouStatuses.has(t.status))
      .map((t) => t.id),
  );
  return (tasks || []).filter(
    (t) => needsYouStatuses.has(t.status) && !wasNeedy.has(t.id),
  );
}

// One notification line per task: "84251cb2 · awaiting approval — <title>".
export function notificationBody(task) {
  const status = (task.status || "").replace(/_/g, " ");
  return `${(task.id || "").slice(0, 8)} · ${status} — ${task.title || ""}`.slice(0, 120);
}
