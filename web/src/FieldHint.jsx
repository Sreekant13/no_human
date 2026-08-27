import { useState } from "react";
import { hostLabel } from "./fieldHelp.js";

// One "ⓘ How to find this" toggle for a field, used by both the onboarding
// wizard card and Settings → Integrations. Static hint (no tooltip library),
// same shape as desktop/token.html: a <button aria-expanded> that reveals a
// <p id={id} className="field-hint"> the input references via aria-describedby.
//
// `text`/`url` come from the server's help catalogue (integrations/help.py).
// When there is no help text the component renders nothing, so a field without
// a catalogue entry simply shows no hint rather than an empty toggle.
export default function FieldHint({ id, text, url }) {
  const [open, setOpen] = useState(false);
  if (!text) return null;
  const host = hostLabel(url);
  return (
    <div className="field-hint-wrap">
      <button
        type="button"
        className="field-hint-toggle"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((v) => !v)}
      >
        <span aria-hidden="true">ⓘ</span> How to find this
      </button>
      {open && (
        <p id={id} className="field-hint">
          {text}
          {url && (
            <>
              {" "}
              <a href={url} target="_blank" rel="noreferrer noopener">
                Open {host || "docs"} ↗
              </a>
            </>
          )}
        </p>
      )}
    </div>
  );
}
