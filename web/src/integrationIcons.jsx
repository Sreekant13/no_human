// Inline brand marks for the Integrations panel — no remote fetches (the
// Electron CSP and the artifact rule both forbid them). Simple, recognizable,
// mono-friendly marks tinted with each integration's brand color.
import { BRAND_COLOR } from "./integrationChip.js";

const MARKS = {
  // Jira — the stacked Atlassian chevrons.
  jira: (c) => (
    <g fill="none" stroke={c} strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 8.5l8 8 8-8" />
      <path d="M8 5l4 4 4-4" />
    </g>
  ),
  // GitHub — an octocat silhouette (head + ears + eyes).
  github: (c) => (
    <g fill={c}>
      <path d="M6 5.5l1.6 2.2A6 6 0 0 1 12 6.4a6 6 0 0 1 4.4 1.3L18 5.5c.9 1.4.7 3 .4 3.8A5.6 5.6 0 0 1 20 13c0 4-2.6 5.1-5 5.4.4.4.7 1 .7 2v2.1h-7.4V20c0-1 .3-1.6.7-2-2.4-.3-5-1.4-5-5.4 0-1.6.6-2.8 1.6-3.7-.3-.8-.5-2.4.4-3.4z" />
    </g>
  ),
  // GitLab — the tanuki as three triangles.
  gitlab: (c) => (
    <g fill={c}>
      <path d="M12 21.5l3.1-9.6H8.9L12 21.5z" opacity="0.95" />
      <path d="M12 21.5L8.9 11.9H4.5L12 21.5z" opacity="0.75" />
      <path d="M12 21.5l3.1-9.6h4.4L12 21.5z" opacity="0.75" />
      <path d="M4.5 11.9L3.3 8.2a.6.6 0 0 1 .2-.7l.9-.7L6 11.9H4.5z" opacity="0.6" />
      <path d="M19.5 11.9l1.2-3.7a.6.6 0 0 0-.2-.7l-.9-.7L18 11.9h1.5z" opacity="0.6" />
      <path d="M6 4.9l1.5 7H16.5l1.5-7-2.5 7H8.5L6 4.9z" opacity="0.9" />
    </g>
  ),
  // Jenkins — a build cog.
  jenkins: (c) => (
    <g fill="none" stroke={c} strokeWidth="2">
      <circle cx="12" cy="12" r="3.2" />
      <path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1" strokeLinecap="round" />
    </g>
  ),
  // CircleCI — the ring + hub.
  circleci: (c) => (
    <g fill="none" stroke={c} strokeWidth="2">
      <circle cx="12" cy="12" r="8.2" />
      <circle cx="12" cy="12" r="2.6" fill={c} stroke="none" />
    </g>
  ),
  // Slack — the octothorpe of rounded bars.
  slack: (c) => (
    <g fill={c}>
      <rect x="8.6" y="3.6" width="2.3" height="16.8" rx="1.15" />
      <rect x="13.1" y="3.6" width="2.3" height="16.8" rx="1.15" />
      <rect x="3.6" y="8.6" width="16.8" height="2.3" rx="1.15" />
      <rect x="3.6" y="13.1" width="16.8" height="2.3" rx="1.15" />
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
