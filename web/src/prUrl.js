// The ONE rule for turning a task's pr_url into a clickable href, shared by all
// three PR surfaces: the board card (Board.jsx), the Outcomes/Stats table row
// (TaskTable.jsx) and the drawer chip (slideOverSummary.js). Each used to carry
// its own guard — startsWith("http") on two of them, which also admitted
// "http:javascript:..." and "httpjavascript:" — so the guards could drift.
// Only a real http(s) URL links; scheme case-insensitive (HTTPS:// is valid);
// everything else (demo-DB local-pr://, javascript:, null) returns null and the
// surface degrades to a text-only badge, never a dead or dangerous link.
export function httpPrUrl(u) {
  return /^https?:\/\//i.test(u || "") ? u : null;
}
