// Inline glyphs for the Integrations panel — no remote fetches (the Electron CSP
// and the artifact rule both forbid them).
//
// These are deliberately GENERIC CATEGORY glyphs — a ticket, a branch, a merge, a
// build, a pipeline, a message — and NOT the vendors' marks. They used to be
// hand-drawn approximations of the real ones (an octocat silhouette, the Slack
// octothorpe, the tanuki, the Atlassian chevrons), which is a trademark problem and
// not a small one: every vendor here publishes a no-modification rule, a redraw is a
// modification however good the likeness, and this file ships inside the DMG.
//
// GitHub and GitLab answer the redraw question by name. GitHub's Octodex FAQ, quoted
// in FULL because the tail of the sentence is doing real work: "You may not use an
// octocat, created by GitHub or by you, for products or merchandise without written
// permission from GitHub." That is a permission requirement scoped to product use,
// not a blanket ban on octocats — but we are a product and we have no such
// permission, so it lands on us either way. Do not quote it truncated at "or by you";
// that states GitHub's rule as stricter than GitHub wrote it. GitLab's brand FAQ:
// "Can I create my own version of the tanuki?" — "No." GitLab forbids third-party
// logo use outright.
//
// COLOUR IS PART OF THE FIX, not a detail. These glyphs used to be painted in each
// vendor's exact brand hex (BRAND_COLOR). A generic shape rendered in the vendor's
// registered colour, directly beside the vendor's name, is exactly the context that
// turns "a gear" into a claim about a specific company — so shape alone was only half
// a cure. Every glyph now paints in ONE neutral app accent (--accent-500, the same
// token the rest of the UI uses), which belongs to us and to nobody else.
//
// A generic glyph beside the vendor's NAME says what the row is without claiming to
// be anyone's mark, and it needs nobody's permission. Do not "improve" these back
// toward the real logos, and do not re-introduce per-vendor colour; if official marks
// are ever wanted here they are the official asset files, unmodified, with permission
// where the vendor requires it. See TRADEMARK.md and THIRD-PARTY-NOTICES.md.

// The single neutral accent every integration glyph paints in. An existing palette
// token from web/src/styles.css — not a per-vendor colour, and not a new one.
const GLYPH_ACCENT = "var(--accent-500)";

const MARKS = {
  // Issue tracker — a ticket with a torn stub.
  jira: (c) => (
    <g fill="none" stroke={c} strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3.5 7.5h17v3a1.8 1.8 0 0 0 0 3.6v2.9h-17v-2.9a1.8 1.8 0 0 0 0-3.6z" />
      <path d="M9.2 10.4h6M9.2 13.6h4" />
    </g>
  ),
  // Issue tracker — a task checklist. Linear had NO glyph and fell through to
  // the plain-circle fallback (read as "broken/unfinished", m3). Generic
  // category mark in the neutral app accent, NOT Linear's logo — same
  // trademark reasoning as every other glyph here (see header + TRADEMARK.md).
  // Distinct from jira's horizontal ticket so the two trackers don't collide.
  linear: (c) => (
    <g fill="none" stroke={c} strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9.5 7h8M9.5 12h8M9.5 17h5" />
      <path d="M4.6 6.3 5.5 7.2 7.1 5.5" />
      <path d="M4.8 12h.01M4.8 17h.01" />
    </g>
  ),
  // Work management — a board with columns. monday.com also had no glyph and
  // hit the same broken-looking fallback (m3). Generic kanban-board mark, not
  // monday.com's logo; distinct from the trackers and the CI pipeline.
  monday: (c) => (
    <g fill="none" stroke={c} strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3.6" y="4.8" width="16.8" height="14.4" rx="1.6" />
      <path d="M9.2 4.8v14.4M14.8 4.8v14.4" />
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
  // Team chat — a GROUP conversation (two people + a speech bubble). Teams used
  // to have no glyph at all, so it fell through to the plain-circle fallback and
  // read as broken. This is a generic collaboration mark in the one neutral app
  // accent, NOT Microsoft's Teams logo — same trademark reasoning as every other
  // glyph in this file (see the header + TRADEMARK.md). It is distinct from
  // slack's single message bubble so the two notification channels don't collide.
  teams: (c) => (
    <g fill="none" stroke={c} strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="9" cy="8" r="2.6" />
      <path d="M4 18.5v-1a5 5 0 0 1 9.2-2.7" />
      <path d="M13.6 11.2a2.1 2.1 0 1 0 2.2-3.4" />
      <path d="M20.5 19.4V18a3.4 3.4 0 0 0-3-3.4" />
    </g>
  ),
};

export const ICON_NAMES = Object.keys(MARKS);

export function IntegrationIcon({ name, size = 22 }) {
  const draw = MARKS[name];
  // Every glyph, every integration, one colour. The svg sets its own `color` so the
  // accent is the same wherever the icon is mounted (the Integrations panel and the
  // onboarding cards set different inherited colours on their wrappers), and the
  // paths draw with currentColor.
  if (!draw) {
    return (
      <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true"
           style={{ color: GLYPH_ACCENT }}>
        <circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" strokeWidth="2" />
      </svg>
    );
  }
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" role="img" aria-label={name}
         style={{ color: GLYPH_ACCENT }}>
      {draw("currentColor")}
    </svg>
  );
}
