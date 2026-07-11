import { useEffect } from "react";

// Close-on-Escape for modal dialogs. The overlay already closes on click; a
// keyboard user needs the same escape route (WCAG / HIG escape-routes). Pass
// `enabled: false` to suppress it mid-flight (e.g. while a submit is in-flight)
// so Escape can't discard work that's already being saved.
export function useEscapeKey(onEscape, enabled = true) {
  useEffect(() => {
    if (!enabled) return;
    function onKey(e) {
      if (e.key === "Escape") onEscape();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onEscape, enabled]);
}
