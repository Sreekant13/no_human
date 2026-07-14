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
