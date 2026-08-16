import { useState, useEffect } from "react";
import { suggestPaths } from "./api.js";

// Directory autocomplete over GET /api/fs/suggest, shared by Settings' add-repo
// and scan-root fields and the composer's free-text repository input. Options
// refresh 150ms after the last keystroke; the `live` guard keeps a stale
// response from landing after unmount. Each mounted instance needs its own
// `listId` — two datalists with one id break the browser's input pairing.
export default function PathInput({
  value, onChange, listId, placeholder,
  className, style, autoFocus = false, ...rest
}) {
  const [opts, setOpts] = useState([]);
  useEffect(() => {
    let live = true;
    const t = setTimeout(async () => {
      const res = await suggestPaths(value);
      if (live) setOpts(res.suggestions || []);
    }, 150);
    return () => { live = false; clearTimeout(t); };
  }, [value]);
  return (
    <>
      <input
        className={className} list={listId} value={value}
        placeholder={placeholder} spellCheck={false} autoFocus={autoFocus}
        onChange={(e) => onChange(e.target.value)}
        style={style}
        {...rest}
      />
      <datalist id={listId} className="ph-no-capture">
        {opts.map((o) => (
          <option key={o.path} value={o.path}>{o.is_repo ? "git repo" : "folder"}</option>
        ))}
      </datalist>
    </>
  );
}
