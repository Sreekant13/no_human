/**
 * Backlog page — the selection algebra, kept pure and framework-free (same
 * idiom as jiraImport.js / boardGroups.js) so the rules below are testable
 * without a renderer.
 *
 * The rules exist because a multi-select over a tracker backlog is one wrong
 * default away from starting an operator's whole sprint:
 *
 *   1. NOTHING is ever pre-checked. `initialSelection()` is the only way to
 *      build a starting selection and it returns an empty list — there is no
 *      "select all by default" path anywhere in this module.
 *   2. A selection is a list of KEYS, not of issue objects, so it survives a
 *      refresh. That makes it possible for the selection to name a ticket that
 *      is no longer listed (closed in Jira, filtered out by a search) — so the
 *      set that actually gets STARTED is always re-derived from the CURRENT
 *      list (`startKeys`), never from the raw selection. A stale key cannot
 *      smuggle a ticket into a start.
 *   3. An already-imported ticket (`issue.imported`, from the browse
 *      endpoint's local-store lookup) is not selectable and is excluded from
 *      the start set even if a key for it is somehow held — the poller dedupes
 *      on (source="jira", external_id=KEY), and a second task for the same
 *      ticket is the duplicate this guard exists to prevent. Starting one
 *      again is still possible, but only through the row's explicit, separate
 *      "Start again" action (Backlog.jsx) — never as part of a bulk start.
 */

/** A ticket that already has a board task. `imported` is the endpoint's
 * {task_id, status, count} block; absent/null means "no task yet". */
export function isImported(issue) {
  return Boolean(issue && issue.imported);
}

/** The issues a bulk start may include: real keyed rows that aren't imported. */
export function startableIssues(issues) {
  return (issues || []).filter((i) => i && i.key && !isImported(i));
}

/** Their keys, in LIST order — the order tickets will be started in. */
export function selectableKeys(issues) {
  return startableIssues(issues).map((i) => i.key);
}

/** The one way to build a starting selection: empty. Opt-in only. */
export function initialSelection() {
  return [];
}

/** Add/remove one key. Non-selectable keys are refused at the source rather
 * than filtered later, so the checkbox state and the start set agree. */
export function toggleKey(selected, key, issues) {
  const current = selected || [];
  if (current.includes(key)) return current.filter((k) => k !== key);
  if (issues && !selectableKeys(issues).includes(key)) return current;
  return [...current, key];
}

/** Select all — the STARTABLE rows only. An imported ticket is never swept in
 * by a bulk affordance. */
export function selectAll(issues) {
  return selectableKeys(issues);
}

/** Clear — the escape hatch that must always exist next to Select all. */
export function clearSelection() {
  return [];
}

/**
 * The set that will actually be started: the intersection of the held
 * selection with the currently listed, startable rows, in list order.
 * Everything on screen (the count, the button label, the confirmation copy)
 * is derived from THIS, so the number the operator reads is the number of
 * tasks that get created.
 */
export function startKeys(selected, issues) {
  const chosen = new Set(selected || []);
  return selectableKeys(issues).filter((k) => chosen.has(k));
}

/** The issues behind `startKeys`, ready to hand to the intake flow. */
export function startIssues(selected, issues) {
  const chosen = new Set(selected || []);
  return startableIssues(issues).filter((i) => chosen.has(i.key));
}

/** Tri-state for the Select all / Clear pair: which control is meaningful. */
export function selectionState(selected, issues) {
  const startable = selectableKeys(issues);
  const chosen = startKeys(selected, issues);
  if (!startable.length) return "empty";
  if (!chosen.length) return "none";
  return chosen.length === startable.length ? "all" : "some";
}

/** The action button's label — it names the count so a mis-click is visible
 * BEFORE it happens, never "Start" alone. */
export function startLabel(count) {
  if (!count) return "Start tasks";
  return count === 1 ? "Start 1 task" : `Start ${count} tasks`;
}

/**
 * N > 1 is not a batch. The intake flow ("five questions") is interactive and
 * per-task, and queueing tickets past it would silently create tasks with no
 * refined spec — a different product than the one the composer implements.
 * So a multi-select starts the tickets ONE AT A TIME through the same flow,
 * and the UI says so before the first question is asked rather than after.
 * Returns null for 0/1 selected — there is nothing to warn about.
 */
export function multiStartNotice(count) {
  if (!count || count < 2) return null;
  return `${count} tickets, one at a time — no_human asks its five questions for each before creating it. Cancelling stops the rest.`;
}

/** Position readout while the queue drains, e.g. "Ticket 2 of 3 · PROJ-7". */
export function queueProgress(index, total, key) {
  if (!total || total < 2) return null;
  return `Ticket ${index + 1} of ${total}${key ? ` · ${key}` : ""}`;
}
