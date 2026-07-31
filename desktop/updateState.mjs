// Where "Later" is remembered.
//
// One small JSON file under the app's userData dir. It holds exactly two
// facts — the version the user deferred, and when we last checked — because a
// preference file that accumulates fields becomes a migration problem, and
// this one is allowed to vanish without consequence.
//
// Every operation swallows its own errors. A read-only home directory, a disk
// that filled, a file someone hand-edited into invalid JSON: none of those are
// reasons to fail an app launch. The cost of losing this file is one extra
// update prompt, so it is an optimization, never a dependency.

import fs from "node:fs";
import path from "node:path";

export const STATE_FILE = "update-state.json";

/** Read persisted update state. Always returns an object, never throws. */
export function readUpdateState(dir) {
  try {
    const raw = fs.readFileSync(path.join(dir, STATE_FILE), "utf8");
    const parsed = JSON.parse(raw);
    // A JSON file can legally contain a string, an array, or null — none of
    // which the caller can spread. Anything but a plain object is "no state".
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return parsed;
  } catch {
    return {};
  }
}

/** Persist update state. Returns whether it landed, for tests and logging. */
export function writeUpdateState(dir, state) {
  try {
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, STATE_FILE),
      JSON.stringify(state ?? {}, null, 2), "utf8");
    return true;
  } catch {
    return false;
  }
}
