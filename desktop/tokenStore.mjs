// Reading and writing the coding credential in ~/.no_human/.env.
//
// Two modes, mirroring the backend's llm.auth_mode (config.py): a Claude
// subscription OAuth token (the default), or the operator's own Anthropic API
// key — the sanctioned opt-in (CLAUDE.md #1). The MODE lives in config.yaml;
// the credential itself lives only in .env, 0600, and never crosses the IPC
// bridge back to a renderer.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const TOKEN_KEY = "CLAUDE_CODE_OAUTH_TOKEN";
export const API_KEY_VAR = "ANTHROPIC_API_KEY";

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
 *  at boot. A pasted API key gets a pointer to the mode that takes it. */
export function validateToken(value) {
  const v = (value ?? "").trim();
  if (!v) return "Paste the token to continue.";
  // Only the API-key shape is redirected. A broader `sk-ant-` test also
  // matched the VALID subscription token and false-accused a correct paste.
  if (/^sk-ant-api/i.test(v)) {
    return "That looks like an API key. This field takes the subscription " +
           "token from `claude setup-token` — to bill your own Anthropic " +
           "account instead, choose the API-key option.";
  }
  if (/\s/.test(v)) return "That value contains a space — check the paste.";
  return "";
}

/** Mode-aware validation: "subscription" keeps validateToken's rules;
 *  "api_key" accepts exactly the Anthropic API-key shape. */
export function validateCredential(value, mode) {
  if (mode !== "api_key") return validateToken(value);
  const v = (value ?? "").trim();
  if (!v) return "Paste the key to continue.";
  if (/\s/.test(v)) return "That value contains a space — check the paste.";
  if (/^sk-ant-oat/i.test(v)) {
    return "That looks like a subscription token. This field takes an " +
           "Anthropic API key (sk-ant-api…) — to use your Claude " +
           "subscription, choose the subscription option.";
  }
  if (!/^sk-ant-api/i.test(v)) {
    return "An Anthropic API key starts with sk-ant-api… — check the paste.";
  }
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
  // Write the key the ACTIVE profile actually reads — writing the bare key
  // while `llm.auth_profile` names another one would report success and still
  // fail to boot.
  return writeEnvVar(tokenVarFor(configuredProfile(home)), value.trim(), home);
}

/** Mode-aware storage: "api_key" writes ANTHROPIC_API_KEY; "subscription"
 *  writes the active profile's token variable. Validation runs first in both
 *  modes — a wrong-shaped credential never touches disk. */
export function writeCredential(value, mode, home = os.homedir()) {
  if (mode !== "api_key") return writeToken(value, home);
  const err = validateCredential(value, "api_key");
  if (err) throw new Error(err);
  return writeEnvVar(API_KEY_VAR, value.trim(), home);
}

function writeEnvVar(key, value, home) {
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
  kept.push(`${key}=${value}`, "");   // always end with a newline

  // writeFileSync's `mode` applies only when CREATING the file, so an existing
  // 0644 .env is hardened by chmodSync alone.
  fs.writeFileSync(p, kept.join("\n"), { mode: 0o600 });
  fs.chmodSync(p, 0o600);
  return p;
}

/** The configured billing mode from ~/.no_human/config.yaml (`llm.auth_mode`).
 *  Missing file, missing block or missing key all mean the default. */
export function configuredAuthMode(home = os.homedir()) {
  try {
    const text = fs.readFileSync(
      path.join(home, ".no_human", "config.yaml"), "utf8");
    let inLlm = false;
    for (const line of text.split("\n")) {
      if (/^llm\s*:/.test(line)) { inLlm = true; continue; }
      if (inLlm) {
        if (/^\S/.test(line)) break;
        const m = line.match(/^\s+auth_mode\s*:\s*["']?([A-Za-z0-9_-]+)/);
        if (m) return m[1];
      }
    }
  } catch { /* no config → default */ }
  return "subscription";
}

/**
 * Upsert `llm.auth_mode` in config.yaml, preserving everything else. Only the
 * MODE goes in config — the credential stays in .env (config.py's
 * _reject_api_key_in_config enforces the same split server-side). Line-based
 * on purpose: the file is generated by yaml.safe_dump (2-space indent), and a
 * yaml library is not available in the shell.
 */
export function setAuthMode(mode, home = os.homedir()) {
  if (mode !== "subscription" && mode !== "api_key") {
    throw new Error(`unknown auth mode: ${mode}`);
  }
  const p = path.join(home, ".no_human", "config.yaml");
  let lines;
  try {
    lines = fs.readFileSync(p, "utf8").split("\n");
  } catch {
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, `llm:\n  auth_mode: ${mode}\n`);
    return p;
  }
  const llmIdx = lines.findIndex((l) => /^llm\s*:/.test(l));
  if (llmIdx === -1) {
    while (lines.length && lines[lines.length - 1].trim() === "") lines.pop();
    lines.push("llm:", `  auth_mode: ${mode}`, "");
    fs.writeFileSync(p, lines.join("\n"));
    return p;
  }
  let end = lines.length;
  for (let i = llmIdx + 1; i < lines.length; i++) {
    if (/^\S/.test(lines[i]) && lines[i].trim() !== "") { end = i; break; }
  }
  let replaced = false;
  for (let i = llmIdx + 1; i < end; i++) {
    const m = lines[i].match(/^(\s+)auth_mode\s*:/);
    if (m) { lines[i] = `${m[1]}auth_mode: ${mode}`; replaced = true; break; }
  }
  if (!replaced) lines.splice(llmIdx + 1, 0, `  auth_mode: ${mode}`);
  fs.writeFileSync(p, lines.join("\n"));
  return p;
}

/** True when the CONFIGURED mode's credential exists — the first-run gate.
 *  api_key mode looks for ANTHROPIC_API_KEY (.env wins, then process env);
 *  subscription mode keeps hasToken's profile-aware logic. */
export function hasCredential(env = process.env, home = os.homedir()) {
  if (configuredAuthMode(home) !== "api_key") return hasToken(env, home);
  try {
    if (parseEnv(fs.readFileSync(envPath(home), "utf8"))[API_KEY_VAR]) return true;
  } catch { /* no .env yet */ }
  return Boolean(env[API_KEY_VAR] && env[API_KEY_VAR].trim());
}
