// Pure mapping helper for the Integrations settings panel's Test-connection
// result — any response shape from POST /api/integrations/{name}/test (or a
// thrown error) maps to exactly one { tone, icon, text }, never null/
// undefined. Kept out of the JSX so the state machine is unit-testable with
// `node --test` (mirrors integrationChip.js's pattern).
//
// Bug this fixes: Integrations.jsx used to render a result only for
// it.healthy === true / === false. An unconfigured-but-ambiently-authenticated
// GitHub/GitLab returns { healthy: null, status: "ambient", detail: "..." }
// (see _check_view, no_human/integrations/__init__.py) — that matched
// neither branch, so the card rendered nothing after a click.

function identityText(status) {
  if (status.detail) return status.detail;
  const who = status.identity ?? status.username ?? status.login;
  return who ? `authenticated as ${who}` : "connected";
}

export function testResultView(status, error) {
  if (error) {
    return { tone: "err", icon: "✕", text: error.message || "test failed" };
  }
  if (typeof status !== "object" || status === null) {
    return { tone: "err", icon: "✕", text: "no result returned by the server" };
  }
  if (status.healthy === true) {
    return { tone: "ok", icon: "✓", text: identityText(status) };
  }
  if (status.healthy === false) {
    return { tone: "err", icon: "✕", text: status.detail || "connection failed" };
  }
  // healthy == null (includes undefined). Ambient CLI auth (github/gitlab
  // when unconfigured but gh/git is already authenticated) is a genuine
  // working state — the chip already calls it "Active via CLI auth"
  // (integrationChip.js:15) — so it renders as success. The ambient probe is
  // deliberately presence-only (_probe_github_ambient never validates on the
  // wire), so there is no username to show here; the detail string stands in
  // for identity instead of inventing one.
  if (status.status === "ambient") {
    return { tone: "ok", icon: "✓", text: status.detail || "available via ambient CLI auth" };
  }
  // Any other healthy == null shape (missing field, unknown status) fails
  // closed to a rendered failure rather than staying silent.
  return { tone: "err", icon: "✕", text: status.detail || "the server reported no health status" };
}
