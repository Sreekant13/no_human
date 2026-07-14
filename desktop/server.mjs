// Server discovery for the desktop shell (Phase E1: attach-only; E2 adds
// ensure/spawn). The HTTP probe is the source of truth — the pidfile is
// advisory and NEVER a kill target (an attached server belongs to the
// operator).

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const DEFAULT_PORT = 8420;

/**
 * The operator can move the server port via ~/.no_human/config.yaml
 * (`server.port`) — the shell must find the same server `nh start` binds
 * (review finding: hardcoding 8420 silently strands configured installs).
 * Minimal block-scoped parse; NH_ORIGIN still wins as the explicit override.
 */
export function configuredPort(
  configPath = path.join(os.homedir(), ".no_human", "config.yaml"),
) {
  try {
    const text = fs.readFileSync(configPath, "utf8");
    let inServer = false;
    for (const line of text.split("\n")) {
      if (/^server\s*:/.test(line)) { inServer = true; continue; }
      if (inServer) {
        if (/^\S/.test(line)) break;               // left the server: block
        const m = line.match(/^\s+port\s*:\s*(\d+)/);
        if (m) return Number(m[1]);
      }
    }
  } catch { /* no config → default */ }
  return DEFAULT_PORT;
}

export const DEFAULT_ORIGIN = `http://127.0.0.1:${configuredPort()}`;

/**
 * Probe the nh server. Resolves "up" | "down".
 * Uses /api/tasks (cheap, always registered) rather than the SPA catch-all,
 * which would 200 even for a half-up static server.
 */
export async function probe(origin = DEFAULT_ORIGIN, timeoutMs = 1500) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(`${origin}/api/tasks`, { signal: ctrl.signal });
    return res.ok ? "up" : "down";
  } catch {
    return "down";
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Poll until the server is up or the deadline passes. Resolves true when up.
 */
export async function waitForServer(origin = DEFAULT_ORIGIN, deadlineMs = 20000,
                                    intervalMs = 500) {
  const t0 = Date.now();
  while (Date.now() - t0 < deadlineMs) {
    if ((await probe(origin)) === "up") return true;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  return false;
}

/** True when a URL belongs to the local no_human server (kept in-window). */
export function isAppOrigin(url, origin = DEFAULT_ORIGIN) {
  try {
    return new URL(url).origin === origin;
  } catch {
    return false;
  }
}

// ---------------------------- E2: ensure/spawn ---------------------------- //

import { execFile, spawn } from "node:child_process";

export const DEFAULT_NH_PATHS = [
  path.join(os.homedir(), ".local", "bin", "nh"),
  "/opt/homebrew/bin/nh",
  "/usr/local/bin/nh",
];

/**
 * Find the `nh` executable the way the operator's shell would. GUI apps on
 * macOS don't inherit the login shell's PATH, so: $NH_BIN → login-shell
 * `command -v nh` → known install locations. Returns "" when not found.
 */
export async function resolveNhBin(env = process.env,
                                   fallbackPaths = DEFAULT_NH_PATHS) {
  if (env.NH_BIN && fs.existsSync(env.NH_BIN)) return env.NH_BIN;
  const viaShell = await new Promise((resolve) => {
    execFile(env.SHELL || "/bin/zsh", ["-lc", "command -v nh"],
             { timeout: 5000 }, (err, stdout) =>
      resolve(err ? "" : stdout.trim()));
  });
  if (viaShell && fs.existsSync(viaShell)) return viaShell;
  for (const p of fallbackPaths) {
    if (fs.existsSync(p)) return p;
  }
  return "";
}

/**
 * Attach to a running server, or spawn `nh start --no-open` and wait for it.
 * Returns {status:"attached"} | {status:"spawned", child} | {status:"failed",
 * reason}. The caller must retain `child`: it is the ONLY legitimate kill
 * target — the pidfile belongs to the operator and is never consulted for
 * killing (design rule from the plan; an attached server is not ours).
 */
export async function ensureServer({
  origin = DEFAULT_ORIGIN,
  spawnTimeoutMs = 20000,
  env = process.env,
  nhArgs = ["start", "--no-open"],
  fallbackPaths = DEFAULT_NH_PATHS,
} = {}) {
  if ((await probe(origin)) === "up") return { status: "attached" };
  const bin = await resolveNhBin(env, fallbackPaths);
  if (!bin) {
    return { status: "failed", reason: "nh-not-found" };
  }
  // Merge over the process env: nh (and a shebang'd test double) needs
  // PATH/HOME; the env param carries only resolution overrides.
  const child = spawn(bin, nhArgs, {
    env: { ...process.env, ...env }, detached: false, stdio: "ignore",
  });
  const spawnErrored = new Promise((resolve) =>
    child.once("error", () => resolve("spawn-error")));
  const raced = await Promise.race(
    [waitForServer(origin, spawnTimeoutMs), spawnErrored]);
  if (raced === "spawn-error" || (await probe(origin)) !== "up") {
    // Deliberately NOT killed (review): a slow-booting nh may be about to
    // win the port bind; killing it races a legitimately-starting server.
    // It is left to finish; the next launch (or error.html's Retry) probes
    // and ATTACHES to it. Arbitration against a concurrent operator start
    // is the OS port bind (nh's pid lock is advisory and check-then-write).
    return {
      status: "failed",
      reason: raced === "spawn-error" ? "spawn-error" : "spawn-timeout",
      child,
    };
  }
  return { status: "spawned", child };
}

/**
 * Stop the server ONLY when this shell spawned it. Attached servers belong
 * to the operator — never touched, never pidfile-killed.
 */
export function stopServer(state) {
  if (!state || state.status !== "spawned" || !state.child) return false;
  try {
    state.child.kill("SIGTERM");
    return true;
  } catch {
    return false;
  }
}
