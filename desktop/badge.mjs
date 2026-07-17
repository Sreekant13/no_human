// Dock/taskbar badge = the needs-you count. The web app already encodes it in
// the window title ("(N) no_human", notifications.js titleWithBadge) — the
// shell parses that instead of re-deriving needs-you, so the badge can never
// drift from the board (same single-source rule as isNeedsYou).
//
// Contract: a title in the app's own format returns its count (0 when clean);
// any OTHER title (the error page, a mid-navigation document title) returns
// null = "no information" — the shell keeps the last truthful badge instead
// of wiping a count that is still true (PR #104 review, low).
export function parseBadgeCount(title) {
  const t = title || "";
  if (t === "no_human") return 0;
  const m = /^\((\d+)\) no_human$/.exec(t);
  return m ? Number(m[1]) : null;
}
