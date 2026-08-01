// Inline glyphs for the Integrations panel — no remote fetches (the Electron CSP
// and the artifact rule both forbid them).
//
// These are deliberately GENERIC CATEGORY glyphs — a ticket, a branch, a merge, a
// build, a pipeline, a message — and NOT the vendors' marks. They used to be
// hand-drawn approximations of the real ones (an octocat silhouette, the Slack
// octothorpe, the tanuki, the Atlassian chevrons), which is a trademark problem and
// not a small one: every vendor here publishes a no-modification rule, a redraw is a
// modification however good the likeness, and this file ships inside the DMG. GitHub
// and GitLab answer the redraw question by name — "You may not use an octocat,
// created by GitHub or by you", and "Can I create my own version of the tanuki? No."
// GitLab forbids third-party logo use outright. See docs/INTEGRATIONS_LEGAL.md §4.3.
//
// A generic glyph beside the vendor's NAME says what the row is without claiming to
// be anyone's mark, and it needs nobody's permission. Do not "improve" these back
// toward the real logos; if official marks are ever wanted here they are the official
// asset files, unmodified, with permission where the vendor requires it.
import { BRAND_COLOR } from "./integrationChip.js";

const MARKS = {
  // Issue tracker — a ticket with a torn stub.
  jira: (c) => (
    <g fill="none" stroke={c} strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3.5 7.5h17v3a1.8 1.8 0 0 0 0 3.6v2.9h-17v-2.9a1.8 1.8 0 0 0 0-3.6z" />
      <path d="M9.2 10.4h6M9.2 13.6h4" />
    </g>
  ),
  // Version control — a branch.
  github: (c) => (
    <g fill="none" stroke={c} strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="7" cy="5.5" r="2.2" />
      <circle cx="7" cy="18.5" r="2.2" />
      <circle cx="17" cy="9.5" r="2.2" />
      <path d="M7 7.7v8.6M17 11.7v.6a3.5 3.5 0 0 1-3.5 3.5H9.6" />
    </g>
  ),
  // Version control — a merge.
  gitlab: (c) => (
    <g fill="none" stroke={c} strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="6.5" cy="5.5" r="2.2" />
      <circle cx="17.5" cy="5.5" r="2.2" />
      <circle cx="12" cy="18.5" r="2.2" />
      <path d="M6.5 7.7v1.1a3.5 3.5 0 0 0 3.5 3.5h4a3.5 3.5 0 0 0 3.5-3.5V7.7M12 12.3v4" />
    </g>
  ),
  // CI — a build cog.
  jenkins: (c) => (
    <g fill="none" stroke={c} strokeWidth="1.9">
      <circle cx="12" cy="12" r="3.2" />
      <path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1" strokeLinecap="round" />
    </g>
  ),
  // CI — a pipeline of stages.
  circleci: (c) => (
    <g fill="none" stroke={c} strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2.6" y="8.8" width="5.4" height="6.4" rx="1.4" />
      <rect x="16" y="8.8" width="5.4" height="6.4" rx="1.4" />
      <path d="M8.6 12h6.8M13.2 9.9 15.5 12l-2.3 2.1" />
    </g>
  ),
  // Notifications — a message.
  slack: (c) => (
    <g fill="none" stroke={c} strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <path d="M20.4 14.2a2 2 0 0 1-2 2H8.9L4.6 19.6V6a2 2 0 0 1 2-2h11.8a2 2 0 0 1 2 2z" />
      <path d="M8.4 8.9h8.2M8.4 12.1h5.4" />
    </g>
  ),
};

export const ICON_NAMES = Object.keys(MARKS);

export function IntegrationIcon({ name, size = 22 }) {
  const draw = MARKS[name];
  const color = BRAND_COLOR[name] || "currentColor";
  if (!draw) {
    return (
      <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" strokeWidth="2" />
      </svg>
    );
  }
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" role="img" aria-label={name}>
      {draw(color)}
    </svg>
  );
}
