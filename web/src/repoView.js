// C3-G3: pure formatting for the "Repository Understanding" panel — the profile
// dict from /api/repo turned into ordered {label, value} rows for display. Kept
// separate from the React component so it is unit-testable without a DOM.

// Ordered so the most useful facts come first; only non-empty fields are shown,
// so an unconfirmed/sparse profile renders cleanly rather than as blank rows.
const FIELDS = [
  ["ecosystem", "Ecosystem"],
  ["test_cmd", "Unit tests"],
  ["integration_test_cmd", "Integration tests"],
  ["lint_cmd", "Lint"],
  ["install_cmd", "Install"],
  ["default_branch", "Default branch"],
  ["vcs_host", "VCS host"],
];

export function profileRows(profile) {
  if (!profile) return [];
  const rows = [];
  for (const [key, label] of FIELDS) {
    const v = profile[key];
    if (v) rows.push({ label, value: String(v) });
  }
  const ci = profile.ci || {};
  if (ci.enabled) {
    rows.push({ label: "Remote CI", value: ci.backend || "gitlab" });
  }
  return rows;
}

// A one-line trust summary: onboarded+proven vs. only-known. Never throws.
export function profileStatus(profile) {
  if (!profile) return "not onboarded — map only";
  const proven = profile.proven && profile.proven.test_cmd;
  if (profile.confirmed && proven) return "onboarded & proven";
  if (proven) return "proven, unconfirmed";
  return "onboarded, unproven";
}
