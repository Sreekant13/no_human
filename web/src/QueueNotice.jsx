// "Ticket 2 of 10 · NO-3" — where the operator is in a multi-ticket run, plus
// the one explicit way out of the whole run.
//
// It is a shared component rather than a string rendered per branch because it
// has to appear in EVERY step of the intake flow. It used to be handed to
// TaskComposer alone, so the readout vanished the moment the scoping questions
// started: the operator spent the longest part of the flow — five rounds of
// questions, then the refined-spec review — with no idea which of their ten
// tickets they were answering about, and no idea how many were left.
//
// `onStopAll` is what makes cancelling safe to scope narrowly (see
// backlogSelection.js): Escape/Cancel drops only the ticket on screen, and
// abandoning the rest is this button, which names the count it discards.
export default function QueueNotice({ notice, remaining = 0, onStopAll = null }) {
  if (!notice) return null;
  return (
    <div className="queue-notice ph-no-capture">
      <span className="queue-notice-text">{notice}</span>
      {onStopAll && remaining > 0 && (
        <button
          type="button"
          className="queue-notice-stop"
          onClick={onStopAll}
          title={`Don't start the remaining ${remaining} ticket${remaining === 1 ? "" : "s"} — they stay open in your tracker`}
        >
          Stop the rest ({remaining})
        </button>
      )}
    </div>
  );
}
