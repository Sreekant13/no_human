import { useState } from "react";
import { deferredItems } from "./onboardingMinimal.js";

// A small checklist icon — inline SVG (CSP forbids remote icon fonts/images),
// matching the sidebar's 16px single-stroke set.
function IconChecklist() {
  return (
    <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 4.2l1.4 1.4L6 3" />
      <path d="M2 11l1.4 1.4L6 9.8" />
      <path d="M8.5 4.3h5.5M8.5 10.7h5.5" />
    </svg>
  );
}

// The minimal path's leftover steps (spec §3 B1), reworked from a board-body
// card into a compact sidebar affordance directly above Settings — real-user
// feedback: the old card "took half the screen", and its rows all fell back to
// the wrong Settings pane. Collapsed it is one navrow-styled button with a
// count badge ("Finish setup · 4"); clicking expands an inline list where each
// row deep-links to its OWN Settings pane and "Done" dismisses it (POST
// /deferred/{step}/done in App). The whole entry is gone once nothing remains.
export default function FinishSetupCard({ deferred, onNavigate, onDone }) {
  const [open, setOpen] = useState(false);
  const items = deferredItems(deferred);
  if (!items.length) return null;
  return (
    // ph-no-capture: item titles are static labels today, but this masks the
    // entry from telemetry capture defensively (and satisfies the content sweep).
    <div className="finish-setup ph-no-capture">
      <button
        type="button"
        className="nh-navrow nh-finish-setup-row"
        aria-haspopup="true"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        title="Finish setting up no_human"
      >
        <span className="nh-navrow-icon" aria-hidden="true"><IconChecklist /></span>
        <span className="nh-navrow-label">Finish setup</span>
        <span className="nh-navrow-badge nh-navrow-badge-alert">{items.length}</span>
      </button>
      {open && (
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
                aria-label={`Mark ${it.title} done`}
              >
                Done
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
