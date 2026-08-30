import { useEffect, useRef } from "react";
import { SHORTCUTS } from "./shortcuts.js";
import { keepFocusInDialog } from "./keepFocusInDialog.js";
import { useEscapeKey } from "./useEscapeKey.js";

// The ⌘/ cheat-sheet. A read-only list (no text entry, nothing to lose), so
// this deliberately skips the Tab-focus-trap + self-heal machinery Settings
// and SlideOver carry for their editable panels — there is exactly one
// focusable control in here, the Close button, so trapping Tab on it would
// be a no-op dressed up as a feature. What it keeps from that shared pattern:
// focus lands on Close when the dialog opens, focus returns to whatever
// opened it on close, Escape closes, and a stray backdrop click can't steal
// focus mid-dialog (keepFocusInDialog — harmless here since nothing is ever
// being typed, but it's the same guard every other overlay carries).
export default function ShortcutsDialog({ onClose, isDesktop }) {
  const closeRef = useRef(null);
  const triggerRef = useRef(null);

  useEffect(() => {
    triggerRef.current = document.activeElement;
    return () => {
      const trigger = triggerRef.current;
      if (trigger && trigger !== document.body && document.contains(trigger) && typeof trigger.focus === "function") {
        trigger.focus();
      }
    };
  }, []);

  useEffect(() => { closeRef.current?.focus(); }, []);

  useEscapeKey(onClose, true);

  const rows = SHORTCUTS.filter((s) =>
    s.when === "always" || s.when === (isDesktop ? "desktop" : "browser"));
  // keys.mac/other is an OS distinction (⌘ vs Ctrl), independent of isDesktop
  // (which is Electron-vs-browser) — a Mac laptop running the board in a
  // regular browser tab still reads "⌘" more naturally than "Ctrl".
  const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || "");

  return (
    <div className="shortcuts-overlay" onMouseDown={keepFocusInDialog}>
      <div
        className="shortcuts-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="shortcuts-dialog-title"
      >
        <div className="shortcuts-dialog-header">
          <h2 id="shortcuts-dialog-title">Keyboard shortcuts</h2>
          <button
            type="button"
            className="shortcuts-dialog-close"
            onClick={onClose}
            ref={closeRef}
            aria-label="Close"
          >
            ×
          </button>
        </div>
        <ul className="shortcuts-list">
          {rows.map((s) => (
            <li key={s.id} className="shortcuts-row">
              <span className="shortcuts-row-label">{s.label}</span>
              <kbd className="shortcuts-kbd">{isMac ? s.keys.mac : s.keys.other}</kbd>
            </li>
          ))}
        </ul>
        <div className="shortcuts-dialog-actions">
          <button type="button" className="btn" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
