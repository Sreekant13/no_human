// The minimal onboarding path (spec §3 B1): finish after picking ONE repo and
// carry the skipped steps on the board as a Finish-setup card. A real user
// wanted to start after choosing a single repo instead of walking eight steps;
// these two predicates are the whole decision, kept out of the JSX so the
// `node --test` harness (no React renderer) can check them directly.

/** True once at least one repo is ticked — the only precondition the "Start
 *  with this repo" button has. */
export function canStartMinimal({ selectedRepos }) {
  return (selectedRepos?.size || 0) >= 1;
}

// The deferred onboarding steps and where each one lands in Settings. `tab` is
// the SettingsOverlay pane key; the affordance calls onNavigate({page:"settings",
// tab}) and App opens the overlay on that pane. Every `tab` here MUST be a real
// Settings pane (Settings.jsx SECTIONS): "docs" and "history" had no pane of
// their own, so both fell through to the generic "projects" fallback (the
// deep-link bug a real user hit). They now resolve to the pane where the work
// actually lives — repo docs/wiki with the repos in Projects; mined AI-history
// and the rules review both in the Rules pane.
const ITEMS = {
  docs:         { title: "Repo docs & wiki", tab: "projects" },
  integrations: { title: "Integrations",     tab: "integrations" },
  history:      { title: "AI history",        tab: "rules" },
  rules:        { title: "Rules review",      tab: "rules" },
};

/** The Finish-setup card's rows, in the server's deferred order. Unknown keys
 *  are dropped rather than rendered as a dead link. */
export function deferredItems(deferred) {
  return (deferred || [])
    .filter((key) => ITEMS[key])
    .map((key) => ({ key, title: ITEMS[key].title, page: "settings", tab: ITEMS[key].tab }));
}
