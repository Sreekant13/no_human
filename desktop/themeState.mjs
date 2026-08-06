// The shell's copy of the board's light/dark choice.
//
// WHY A FILE AT ALL: the choice's real home is renderer localStorage
// ("nh-theme", web/src/App.jsx), and the main process cannot read that — it has
// to pick a window's backgroundColor and its Windows title-bar colours BEFORE
// any renderer exists. Without a copy the shell can only ever pre-paint one
// fixed colour, which means whichever theme is not the default gets a flash of
// the other one on every launch. So the renderer mirrors every choice here and
// startup reads it back.
//
// This is a CACHE, not the truth. localStorage still decides what the board
// renders; losing this file costs exactly one wrong-coloured first frame, after
// which the renderer rewrites it. That is why every operation swallows its own
// errors — a read-only home or a hand-edited file must never fail a launch —
// and why the file holds one field and can be deleted at any time. Same
// contract, and the same reasoning, as updateState.mjs beside it.

import fs from "node:fs";
import path from "node:path";

export const THEME_FILE = "theme.json";
/** What the board itself defaults to when nothing is stored (App.jsx). */
export const DEFAULT_THEME = "dark";

/**
 * The two themes, and nothing else. Returns null for anything unrecognised so
 * callers can tell "not a theme" from "a theme" — nh:set-theme is exposed to
 * board content, and this is the only thing standing between that content and
 * a value written to disk.
 */
export function normalizeTheme(value) {
  return value === "dark" || value === "light" ? value : null;
}

/**
 * The window colours for a theme: the pre-paint background, and the glyph
 * colour for the Windows title-bar overlay that draws minimise/maximise/close.
 * One source for both, because they were two separate literals on one line and
 * a mismatch there is an invisible or unreadable control strip.
 */
export function themeColors(theme) {
  return theme === "light"
    ? { bg: "#F4F5F7", symbol: "#1A1D23" }
    : { bg: "#0F1117", symbol: "#E6E8EC" };
}

/** The persisted theme, or DEFAULT_THEME. Never throws, never returns junk. */
export function readTheme(dir) {
  try {
    const parsed = JSON.parse(fs.readFileSync(path.join(dir, THEME_FILE), "utf8"));
    // A JSON file can legally hold a string, an array or null. Anything but a
    // plain object carrying a known theme is "nothing stored".
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return DEFAULT_THEME;
    return normalizeTheme(parsed.theme) ?? DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

/** Persist a theme. Returns whether it landed, for tests and for the caller. */
export function writeTheme(dir, theme) {
  const value = normalizeTheme(theme);
  if (!value) return false;
  try {
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, THEME_FILE),
      JSON.stringify({ theme: value }, null, 2), "utf8");
    return true;
  } catch {
    return false;
  }
}
