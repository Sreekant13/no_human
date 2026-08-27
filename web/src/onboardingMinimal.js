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

// The deferred onboarding steps and where each one lives in Settings. `tab` is
// the SettingsOverlay pane key; the board has no hash router, so the card calls
// onNavigate({page:"settings", tab}) and App opens the overlay on that pane.
const ITEMS = {
  docs:         { title: "Repo docs & wiki", tab: "docs" },
  integrations: { title: "Integrations",     tab: "integrations" },
  history:      { title: "AI history",        tab: "history" },
  rules:        { title: "Rules review",      tab: "rules" },
};

/** The Finish-setup card's rows, in the server's deferred order. Unknown keys
 *  are dropped rather than rendered as a dead link. */
export function deferredItems(deferred) {
  return (deferred || [])
    .filter((key) => ITEMS[key])
    .map((key) => ({ key, title: ITEMS[key].title, page: "settings", tab: ITEMS[key].tab }));
}
