/**
 * The Backlog page reads TWO trackers. This is the part that decides which of
 * them it asks, how their answers combine, and — the part that had already
 * gone wrong once — what the page is allowed to SAY about the ones it does not
 * show.
 *
 * The page used to explain Linear's absence with "the Linear side has no issue
 * listing yet". That was not true: LinearAdapter.search() has been a working
 * paginating GraphQL listing the whole time; only the HTTP route was missing.
 * A UI explanation that states a fact about the code is a claim, and this
 * module exists so every such claim is derived from what the server actually
 * answered rather than written by hand into JSX.
 *
 * Kept pure and framework-free (same idiom as backlogSelection.js) so all of
 * that is provable without a renderer.
 */

/** The trackers this product can list, in display order. */
export const TRACKERS = [
  { name: "jira", label: "Jira" },
  { name: "linear", label: "Linear" },
];

const labelOf = (name) => (TRACKERS.find((t) => t.name === name) || {}).label || name;

/** The tracker names the server reports as configured, in display order. */
export function configuredTrackers(registry) {
  const list = (registry && registry.integrations) || [];
  return TRACKERS
    .filter((t) => list.some((i) => i && i.name === t.name && i.configured))
    .map((t) => t.name);
}

/**
 * Combine the per-tracker answers into one list plus the failures.
 *
 * `results` is `[{tracker, issues, error}]`. A tracker that FAILED contributes
 * an error and no rows — never an empty list. Folding a failure into "no
 * tickets" is the same lie the not-configured/unreachable confusion is: the
 * backlog is not empty, it could not be read, and only one of those sentences
 * is safe to show someone deciding what to work on.
 *
 * Rows are ordered by `updated`, newest first, so a merged two-tracker list
 * reads as one backlog rather than as two lists stapled together. Rows with no
 * timestamp sort last (they cannot claim recency).
 */
export function mergeTrackerResults(results) {
  const issues = [];
  const errors = [];
  for (const r of results || []) {
    if (!r) continue;
    if (r.error) errors.push({ tracker: r.tracker, label: labelOf(r.tracker), message: r.error });
    else for (const i of r.issues || []) if (i && i.key) issues.push(i);
  }
  issues.sort((a, b) => {
    const ta = Date.parse(a.updated || "");
    const tb = Date.parse(b.updated || "");
    if (Number.isNaN(ta) && Number.isNaN(tb)) return 0;
    if (Number.isNaN(ta)) return 1;
    if (Number.isNaN(tb)) return -1;
    return tb - ta;
  });
  return { issues, errors };
}

/**
 * The one line under the filter box that says which trackers these tickets
 * came from — and, when one is missing, the TRUE reason.
 *
 * "not connected" is the only claim made about a tracker that is off: whether
 * this product can read it is not the question the operator is answering, and
 * the last time this line volunteered an answer to that question it was wrong.
 */
export function sourcesLine(configured) {
  const on = configured || [];
  const off = TRACKERS.filter((t) => !on.includes(t.name)).map((t) => t.label);
  if (!on.length) return null;
  const onLabels = on.map(labelOf);
  const shown = onLabels.length === 1 ? onLabels[0] : `${onLabels.slice(0, -1).join(", ")} and ${onLabels.slice(-1)}`;
  if (!off.length) return `Open tickets from ${shown}.`;
  return `Open tickets from ${shown} — ${off.join(" and ")} ${off.length === 1 ? "is" : "are"} not connected.`;
}

/** The header/subtitle when nothing is configured at all. */
export function noTrackerMessage() {
  return "No tracker is connected.";
}
