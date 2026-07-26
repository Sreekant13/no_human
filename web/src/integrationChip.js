// Pure mapping helpers for the Integrations settings panel — status → chip and
// integration → labels/brand color. Kept out of the JSX so the state machine is
// unit-testable with `node --test`.

// The status chip an IntegrationStatus renders. Five states:
//   unconfigured (neutral) · ambient — no stored config, but the operator's
//   own CLI (gh/git) is already authenticated for it (SCRUM-81, `status`
//   field from GET /api/integrations) (ambient) · configured-but-untested
//   (ok) · connected after a passing test (ok) · error after a failing test
//   (error). Ambient is checked before the configured/unconfigured split:
//   the backend never marks a stored/configured integration "ambient" (see
//   list_integrations_with_ambient), so this can't shadow a real config.
export function statusChip(it) {
  if (!it) return { label: "Unconfigured", tone: "neutral" };
  if (it.status === "ambient") return { label: "Active via CLI auth", tone: "ambient" };
  if (!it.configured) return { label: "Unconfigured", tone: "neutral" };
  if (it.healthy === false) return { label: "Error", tone: "error" };
  if (it.healthy === true) return { label: "Connected", tone: "ok" };
  return { label: "Configured", tone: "ok" };
}

export const NAME_LABEL = {
  jira: "Jira", github: "GitHub", gitlab: "GitLab",
  jenkins: "Jenkins", circleci: "CircleCI", slack: "Slack",
};

// Where each integration is configured (shown in the expand, so the operator
// knows which file/section to edit — there is one source of truth per setting).
export const CONFIG_HINT = {
  jira: "config.yaml → integrations.jira",
  circleci: "config.yaml → ci (backend: circleci) + integrations.circleci",
  github: "config.yaml → ci (backend: github_actions)",
  gitlab: "config.yaml → ci (backend: gitlab)",
  jenkins: "config.yaml → ci (backend: jenkins)",
  slack: "config.yaml → notifications.slack_webhook_url",
};

export const KIND_LABEL = {
  issue_tracker: "Issue tracker",
  vcs: "Version control",
  ci: "CI / CD",
  notifications: "Notifications",
};

// Brand accent per integration (the mark's fill). Fixed hex — theme-independent,
// so the same recognizable color reads in both light and dark.
export const BRAND_COLOR = {
  jira: "#2684FF",
  github: "#6E7681",
  gitlab: "#FC6D26",
  jenkins: "#D33833",
  circleci: "#00CF69",
  slack: "#E01E5A",
};

// Which ~/.no_human/.env key backs each integration's secret (shown, never the
// value), so the config-expand can tell the operator exactly what to set.
export const SECRET_ENV_KEY = {
  jira: "JIRA_API_TOKEN",
  circleci: "CIRCLECI_TOKEN",
  slack: "(webhook in config)",
};
