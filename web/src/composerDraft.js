// Draft recovery for the New Task composer.
//
// The defect this exists for: a friend lost roughly 2500 typed characters —
// a full task description — to an accidental close of the composer, with no
// way to get it back. This module is the pure persistence half of the fix:
// it saves the plain form fields to localStorage (debounced, so every
// keystroke doesn't hit storage), loads them back, clears them, and merges a
// recovered draft with whatever seed the onboarding flow passes in. Wiring
// this into TaskComposer.jsx (a restore banner, calling saveDraft on change,
// clearing on submit) is a separate ticket — this module has no React and no
// component imports.
//
// Only plain form fields are ever persisted. DRAFT_FIELDS is an ALLOWLIST,
// not a denylist: the persisted object is built by *picking* these keys out
// of whatever is passed in, so anything else — attachments, project ids,
// tokens, api keys, passwords — is dropped by construction rather than by a
// filter that has to anticipate every credential-shaped name.

export const DRAFT_KEY = "nh-composer-draft";

// Matches the repo's keystroke-coalescing interval (web/src/backlogJira.test.mjs,
// "typing is debounced 300ms").
export const DRAFT_DEBOUNCE_MS = 300;

export const DRAFT_FIELDS = ["prompt", "title", "repoPath", "kind", "priority", "acceptance"];

function defaultStorage() {
  // Resolved at call time, not at module load, so importing this module under
  // `node --test` (where globalThis.localStorage is undefined) never throws.
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

function pickFields(fields) {
  const picked = {};
  if (!fields || typeof fields !== "object") return picked;
  for (const key of DRAFT_FIELDS) {
    const value = fields[key];
    if (value === undefined) continue;
    const type = typeof value;
    if (type === "string" || type === "boolean" || type === "number") {
      picked[key] = value;
    }
  }
  return picked;
}

let pendingTimer = null;
let pendingClearTimer = clearTimeout;

/** Undebounced write. Never throws — a quota/private-mode failure must never
 *  break typing. Returns true when it persisted, false otherwise (including
 *  when storage is unavailable). */
export function saveDraftNow(fields, storage = defaultStorage()) {
  if (!storage) return false;
  try {
    storage.setItem(DRAFT_KEY, JSON.stringify(pickFields(fields)));
    return true;
  } catch {
    return false;
  }
}

/** Cancels any armed debounced save without writing it. */
export function cancelPendingDraft(clearTimer = pendingClearTimer) {
  if (pendingTimer !== null) {
    try {
      clearTimer(pendingTimer);
    } catch {
      // ignore — best effort cancel
    }
    pendingTimer = null;
  }
}

/** Writes the pending draft (if any) immediately and cancels its timer. */
export function flushDraft(fields, storage = defaultStorage(), clearTimer = pendingClearTimer) {
  cancelPendingDraft(clearTimer);
  return saveDraftNow(fields, storage);
}

/** Debounced persist: N rapid calls within delayMs produce exactly one write,
 *  carrying the LAST fields passed. Returns the armed timer handle. */
export function saveDraft(
  fields,
  { storage = defaultStorage(), delayMs = DRAFT_DEBOUNCE_MS, setTimer = setTimeout, clearTimer = clearTimeout } = {},
) {
  cancelPendingDraft(clearTimer);
  pendingClearTimer = clearTimer;
  pendingTimer = setTimer(() => {
    pendingTimer = null;
    saveDraftNow(fields, storage);
  }, delayMs);
  return pendingTimer;
}

/** Reads the saved draft. Returns null for: no storage, no key, empty value,
 *  unparsable JSON, or a parsed value that isn't a plain non-array object.
 *  Never throws, for any stored bytes. */
export function loadDraft(storage = defaultStorage()) {
  if (!storage) return null;
  let raw;
  try {
    raw = storage.getItem(DRAFT_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  return pickFields(parsed);
}

/** Removes the saved draft and cancels any save already in flight — without
 *  the cancel, a clear could be immediately undone by a timer armed just
 *  before it (submit, then a 300ms-old pending save resurrects the draft). */
export function clearDraft(storage = defaultStorage()) {
  cancelPendingDraft();
  if (!storage) return false;
  try {
    storage.removeItem(DRAFT_KEY);
    return true;
  } catch {
    return false;
  }
}

/** Merges a recovered draft with the seed the onboarding flow passes in: any
 *  field PRESENT in `initial` (key exists, value not undefined/null) wins
 *  over the drafted value. Pure — mutates neither argument. */
export function mergeWithSeed(draft, initial) {
  const merged = pickFields(draft);
  if (initial && typeof initial === "object") {
    for (const key of DRAFT_FIELDS) {
      const value = initial[key];
      if (value !== undefined && value !== null) {
        merged[key] = value;
      }
    }
  }
  return merged;
}
