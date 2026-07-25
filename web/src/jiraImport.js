/**
 * Task 1.6 — Import from Jira: build the composer prefill from a picked issue.
 *
 * Pure and framework-free so it's unit-testable without a renderer (like
 * promptSplit.js / prRefs.js). TaskComposer.jsx feeds the result into the
 * SAME single `prompt` textarea the typed-task path uses — splitPrompt.js
 * later re-derives title/description from it, so the grill flow never has to
 * know an import happened.
 */

/** title = "KEY: summary"; description = the issue's own description plus a
 * traceability line back to the ticket (always appended, even when the issue
 * has no description of its own). */
export function prefillFromIssue(issue) {
  const title = `${issue.key}: ${issue.summary}`;
  const description = `${issue.description || ""}\n\nImported from Jira: ${issue.url}`;
  return { title, description };
}

/** The composer's one prompt textarea, built so promptSplit.js's "first line
 * is the title" rule recovers the same title/description pair. */
export function promptFromIssue(issue) {
  const { title, description } = prefillFromIssue(issue);
  return `${title}\n\n${description}`;
}

/**
 * M1 — status chips must encode STATE, not just show text. Jira exposes a
 * status CATEGORY server-side ("To Do" / "In Progress" / "Done"), but
 * /api/integrations/jira/issues only returns the status NAME. Changing that
 * would be a backend round-trip + an API contract change for one chip's
 * colour, so instead this derives the category client-side from the name.
 * Unrecognised / custom-workflow names fall through to "unknown" — still
 * legible via the plain status text, just with no colour claim we can't back.
 */
export function jiraStatusCategory(status) {
  const s = (status || "").trim().toLowerCase();
  if (["done", "closed", "resolved"].includes(s)) return "done";
  if (["in progress", "in review", "review"].includes(s)) return "active";
  if (["to do", "todo", "backlog", "open", "new"].includes(s)) return "todo";
  return "unknown";
}

// One token pair per category, reusing the EXISTING semantic ramp (already
// bridged for both themes, already used inline in this file's error states —
// see the jiraError alert below) rather than inventing new CSS vars. Text sits
// on the low-opacity "-dim" fill of its own colour, the same combination the
// error box already relies on; measured contrast holds >=4.5:1 in both themes
// (dark 4.92-7.54:1, light 4.61-5.89:1).
const CATEGORY_TOKENS = {
  done: { color: "var(--green)", background: "var(--green-dim)", borderColor: "var(--green)" },
  active: { color: "var(--blue)", background: "var(--blue-dim)", borderColor: "var(--blue)" },
  todo: { color: "var(--purple)", background: "var(--purple-dim)", borderColor: "var(--purple)" },
};

/** Inline style for a Jira status chip, or `null` for "unknown" so the caller
 * keeps its existing neutral (bg-panel/border-line/text-muted) className. */
export function jiraStatusChipStyle(status) {
  return CATEGORY_TOKENS[jiraStatusCategory(status)] || null;
}

/** The dedup key sent to the backend as external_id, captured when an issue is
 * picked. Pure so the pick-time capture + grill round-trip stay testable. */
export function externalIdFromIssue(issue) {
  return issue?.key ?? null;
}

/**
 * SCRUM-18 — accidental re-import trap. `issue.imported` (from the browse
 * endpoint's local-store lookup) says a board task already exists for this
 * ticket. Map the board TASK status (no_human's own TaskStatus enum values,
 * NOT a Jira status name) to the same done/active/todo/unknown ramp
 * jiraStatusCategory uses, plus a "warn" tone for terminal-bad states and
 * duplicate external_ids — reusing CATEGORY_TOKENS, no new CSS vars.
 */
export function importedStatusCategory(status) {
  const s = (status || "").trim().toLowerCase();
  if (s === "done") return "done";
  if (["failed", "blocked", "escalated"].includes(s)) return "warn";
  if (
    ["pending", "context", "planning", "implementing", "reviewing", "testing",
      "awaiting_approval", "compound_parent", "awaiting_input", "paused_quota"].includes(s)
  ) {
    return "active";
  }
  return "unknown";
}

const WARN_TOKENS = { color: "var(--red)", background: "var(--red-dim)", borderColor: "var(--red)" };

/** Inline style for the "imported" chip, mirroring jiraStatusChipStyle. */
export function importedChipStyle(status) {
  const category = importedStatusCategory(status);
  if (category === "warn") return WARN_TOKENS;
  return CATEGORY_TOKENS[category] || null;
}

/**
 * SCRUM-3 — the picker never told the operator whether it was browsing ALL
 * open tickets (empty query) or a filtered match, nor whether the list was
 * truncated at the request limit. `count` is `jiraResults.length`; `limit` is
 * the request limit passed to searchJiraIssues (must match api.js's default
 * so "first N" is never a lie). Returns `null` for zero results — the
 * empty-state message (jiraEmptyMessage) speaks instead, so the header never
 * says "Showing 0 open tickets".
 */
export function jiraResultHeader(query, count, limit) {
  if (!count) return null;
  const q = (query || "").trim();
  if (q) {
    // At the limit the match set is almost certainly truncated too (review
    // finding: "50 matching tickets" at exactly 50 was the lie the limit
    // comment promises to avoid).
    if (count >= limit) return `First ${limit} matching tickets — type to narrow`;
    return `${count} matching ticket${count === 1 ? "" : "s"}`;
  }
  if (count >= limit) return `Showing first ${limit} open tickets — type to narrow`;
  return `Showing ${count} open ticket${count === 1 ? "" : "s"} — type to filter`;
}

/** Zero-results copy: an empty BROWSE (no query — nothing was matched
 * against) reads differently from a filtered search that matched nothing. */
export function jiraEmptyMessage(query) {
  return (query || "").trim() ? "No matching tickets." : "No open tickets in this project.";
}

/** `issue.updated` is server data — a malformed/missing value must never
 * render `new Date(x).toLocaleDateString()`'s "Invalid Date" string. Returns
 * `null` (caller omits the whole date segment) unless `updated` parses to a
 * real date. */
export function formatIssueUpdated(updated) {
  if (!updated) return null;
  const d = new Date(updated);
  if (Number.isNaN(d.getTime())) return null;
  return `Updated ${d.toLocaleDateString()}`;
}

/** Build the "imported" chip's {label, style, className}, or `null` when the
 * ticket has no board task yet — the caller renders nothing in that case.
 * Import stays clickable either way; this only ever ADDS visible state, it
 * never disables the row (SCRUM-18: re-imports stay possible, just no
 * longer accidental). A count > 1 (duplicate external_ids — a data
 * integrity bug, not something this endpoint cleans up) escalates to a
 * review warning regardless of the underlying status. */
export function importedChip(imported) {
  if (!imported) return null;
  const { status, count = 1 } = imported;
  if (count > 1) {
    return {
      label: `imported ×${count} — review`,
      style: WARN_TOKENS,
      className: "font-medium",
    };
  }
  const style = importedChipStyle(status);
  return {
    label: `imported — ${status}`,
    style,
    className: style ? "font-medium" : "border-line bg-panel text-text-muted",
  };
}
