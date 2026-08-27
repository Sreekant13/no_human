import { deferredItems } from "./onboardingMinimal.js";

// The board-side other half of the minimal path (spec §3 B1): when onboarding
// finished after one repo, the steps the user skipped ride here as a plain
// `.card` — no new visual world. Each row deep-links into the matching Settings
// pane; "Done" dismisses it (POST /deferred/{step}/done in App). The card is
// gone once nothing is deferred.
export default function FinishSetupCard({ deferred, onNavigate, onDone }) {
  const items = deferredItems(deferred);
  if (!items.length) return null;
  return (
    // ph-no-capture: the item titles are static labels today, but this masks the
    // card from telemetry capture defensively (and satisfies the content sweep).
    <div className="card finish-setup-card ph-no-capture">
      <h3>Finish setup</h3>
      <p className="finish-setup-lede">Finish setup — optional. Each item opens in Settings.</p>
      <ul className="finish-setup-list">
        {items.map((it) => (
          <li key={it.key}>
            <button
              type="button"
              className="finish-setup-open"
              onClick={() => onNavigate({ page: it.page, tab: it.tab })}
            >
              {it.title}
            </button>
            <button
              type="button"
              className="finish-setup-done"
              onClick={() => onDone(it.key)}
            >
              Done
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
