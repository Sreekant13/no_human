// Reading and writing the Claude OAuth token in ~/.no_human/.env.
//
// This is the ONLY credential no_human accepts: an OAuth subscription token.
// An ANTHROPIC_API_KEY would silently bill the metered API, so it is never
// read, never written, and never offered here.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const TOKEN_KEY = "CLAUDE_CODE_OAUTH_TOKEN";

export function envPath(home = os.homedir()) {
  return path.join(home, ".no_human", ".env");
}

/** Parse KEY=VALUE lines. Later wins, matching dotenv; comments preserved by
 *  writeToken, which edits lines rather than re-serialising this map. */
export function parseEnv(text) {
  const out = {};
  for (const line of text.split("\n")) {
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
    if (m) out[m[1]] = m[2].trim();
  }
  return out;
}

/**
 * The .env variable holding a profile's token, mirroring
 * config.profile_token_var: the default profile uses the bare key, a named one
 * gets an upper-cased suffix. Without this, an operator on `nh auth use
 * personal` is nagged for a credential they already have.
 */
export function tokenVarFor(profile) {
  const p = (profile || "default").trim().toLowerCase();
  return p === "default" ? TOKEN_KEY : `${TOKEN_KEY}_${p.toUpperCase()}`;
}

/** The configured auth profile from ~/.no_human/config.yaml (`llm.auth_profile`). */
export function configuredProfile(home = os.homedir()) {
  try {
    const text = fs.readFileSync(
      path.join(home, ".no_human", "config.yaml"), "utf8");
    let inLlm = false;
    for (const line of text.split("\n")) {
      if (/^llm\s*:/.test(line)) { inLlm = true; continue; }
      if (inLlm) {
        if (/^\S/.test(line)) break;
        const m = line.match(/^\s+auth_profile\s*:\s*["']?([A-Za-z0-9_-]+)/);
        if (m) return m[1];
      }
    }
  } catch { /* no config → default profile */ }
  return "default";
}

/**
 * True when a usable token exists for the ACTIVE profile. The .env file wins
 * over the process environment, mirroring config.py:167 ("`.env` wins over an
 * inherited token: it is the curated source").
 */
export function hasToken(env = process.env, home = os.homedir()) {
  const key = tokenVarFor(configuredProfile(home));
  try {
    if (parseEnv(fs.readFileSync(envPath(home), "utf8"))[key]) return true;
  } catch { /* no .env yet */ }
  return Boolean(env[key] && env[key].trim());
}

/** Reject values that cannot be a token before touching disk — an empty or
 *  whitespace-only write would produce a file that looks configured and fails
 *  at boot. Also refuses a pasted API key outright. */
export function validateToken(value) {
  const v = (value ?? "").trim();
  if (!v) return "Paste the token to continue.";
  // Only the API-key shape is rejected. A broader `sk-ant-` test also matched
  // the VALID subscription token and false-accused a correct paste.
  if (/^sk-ant-api/i.test(v)) {
    return "That looks like an API key. no_human runs on a subscription " +
           "token from `claude setup-token` — an API key would bill the " +
           "metered API instead of your subscription.";
  }
  if (/\s/.test(v)) return "That value contains a space — check the paste.";
  return "";
}

/**
 * Write the token, PRESERVING every other line. Integration secrets (Jira,
 * CircleCI…) live in this same file, so a clobbering write would silently
 * disconnect them. Drops every existing line for this key and appends the new
 * one last. The file is created 0600 and re-chmodded on every write.
 */
export function writeToken(value, home = os.homedir()) {
  const err = validateToken(value);
  if (err) throw new Error(err);
  const token = value.trim();
  // Write the key the ACTIVE profile actually reads — writing the bare key
  // while `llm.auth_profile` names another one would report success and still
  // fail to boot.
  const key = tokenVarFor(configuredProfile(home));
  const p = envPath(home);
  fs.mkdirSync(path.dirname(p), { recursive: true });

  let lines = [];
  try {
    lines = fs.readFileSync(p, "utf8").split("\n");
  } catch { /* first run — no file yet */ }

  // Remove EVERY existing occurrence, not just the first: dotenv (and parseEnv)
  // are later-wins, so replacing only the first would leave a stale duplicate
  // that still beats the new value while the UI reports success.
  const re = new RegExp(`^\\s*${key}\\s*=`);
  const kept = lines.filter((l) => !re.test(l));
  while (kept.length && kept[kept.length - 1].trim() === "") kept.pop();
  kept.push(`${key}=${token}`, "");   // always end with a newline

  // writeFileSync's `mode` applies only when CREATING the file, so an existing
  // 0644 .env is hardened by chmodSync alone.
  fs.writeFileSync(p, kept.join("\n"), { mode: 0o600 });
  fs.chmodSync(p, 0o600);
  return p;
}
