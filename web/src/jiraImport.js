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
