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
import { canSignal, hasExited, ownsChild } from "./serverOwnership.mjs";

export const DEFAULT_NH_PATHS = [
  path.join(os.homedir(), ".local", "bin", "nh"),
  "/opt/homebrew/bin/nh",
  "/usr/local/bin/nh",
];

/**
 * The `nh` frozen by packaging/build-installer.sh and shipped in the app's
 * Resources (electron-builder extraResources). Present only in a packaged
 * build: unpackaged, `process.resourcesPath` points inside node_modules/electron
 * and has no nh-server dir, and under plain `node --test` it is undefined —
 * both cases return "" so resolution falls through to a developer's own nh.
 */
export function bundledNhPath(resourcesPath = process.resourcesPath) {
  if (!resourcesPath) return "";
  const p = path.join(resourcesPath, "nh-server", "nh");
  return fs.existsSync(p) ? p : "";
}

/**
 * Find the `nh` executable the way the operator's shell would. GUI apps on
 * macOS don't inherit the login shell's PATH, so: $NH_BIN → the bundled server
 * → login-shell `command -v nh` → known install locations. Returns "" when not
 * found.
 *
 * NH_BIN stays ahead of the bundle: it is the documented escape hatch for
 * pointing a packaged app at a working tree, and demoting it would silently
 * pin such installs to the frozen copy.
 */
export async function resolveNhBin(env = process.env,
                                   fallbackPaths = DEFAULT_NH_PATHS,
                                   bundled = bundledNhPath()) {
  if (env.NH_BIN && fs.existsSync(env.NH_BIN)) return env.NH_BIN;
  if (bundled) return bundled;
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
 * Where Claude Code actually installs. The Agent SDK resolves its CLI with
 * `shutil.which("claude")` plus a hardcoded list, and no_human never passes
 * cli_path — so if `claude` is not on the spawned server's PATH, EVERY task
 * dies with CLINotFoundError while the board itself looks perfectly healthy.
 *
 * That is not hypothetical on macOS: a GUI app inherits launchd's PATH
 * (/usr/bin:/bin:/usr/sbin:/sbin), not the login shell's, so the operator's own
 * ~/.local/bin/claude is invisible to a packaged build. /opt/homebrew/bin is
 * worse still — it is in neither launchd's PATH nor the SDK's fallback list.
 */
export const CLI_HINT_DIRS = [
  "/opt/homebrew/bin",                                   // Homebrew, Apple Silicon
  "/usr/local/bin",                                      // Homebrew, Intel
  path.join(os.homedir(), ".local", "bin"),
  path.join(os.homedir(), ".npm-global", "bin"),
  path.join(os.homedir(), ".claude", "local"),
  path.join(os.homedir(), ".yarn", "bin"),
];

/**
 * Append missing directories to a PATH. APPEND, never prepend: nh shells out to
 * git/gh/pytest, and putting user-writable dirs first would let a stray binary
 * shadow the system one. Non-existent dirs are skipped so PATH stays honest.
 */
export function mergePath(basePath, extraDirs = CLI_HINT_DIRS,
                          exists = fs.existsSync) {
  const parts = String(basePath || "").split(":").filter(Boolean);
  const seen = new Set(parts);
  for (const dir of extraDirs) {
    if (!seen.has(dir) && exists(dir)) { parts.push(dir); seen.add(dir); }
  }
  return parts.join(":");
}

/**
 * Bounded accumulator for a child's stdout+stderr. Capped so a chatty/looping
 * process cannot grow this without bound over a long-lived spawn — only the
 * TAIL matters for diagnosing a launch failure.
 *
 * `stop()` ends the capture once startup is confirmed (SCRUM-49): the buffer
 * is only useful for diagnosing the failure window, so it is dropped and
 * further chunks are discarded. It deliberately does NOT touch the
 * underlying stream — see the `stop`-caller in ensureServer for why closing
 * the pipe's read end here would break the still-running server instead.
 */
export function makeOutputCapture(maxChars = 8000) {
  let buf = "";
  let capturing = true;
  return {
    add(chunk) {
      if (!capturing) return;
      buf += String(chunk);
      if (buf.length > maxChars) buf = buf.slice(buf.length - maxChars);
    },
    text() { return buf; },
    stop() { capturing = false; buf = ""; },
    get capturing() { return capturing; },
  };
}

/**
 * Reduce captured output to something small enough for a URL query parameter
 * (~2KB practical max) while keeping the part most likely to explain a
 * failure: the last few lines. Mirrors nh start's own console output, which
 * prints its diagnosis last, right before exiting.
 */
export function tailDetail(text, maxLines = 10, maxChars = 500) {
  const trimmed = String(text || "").trim();
  if (!trimmed) return "";
  const tail = trimmed.split("\n").slice(-maxLines).join("\n");
  if (tail.length <= maxChars) return tail;
  return "[truncated…] " + tail.slice(tail.length - maxChars);
}

/**
 * Classify captured nh-start output against the two backend-check failures
 * `agent/backend_check.py` (via `_assert_backend_usable`/`_bootstrap` in
 * cli/commands.py) diagnoses: a missing `claude` CLI, or no OAuth token on
 * file. Matched on the exact phrases those code paths print, so a genuine
 * match is never confused with an unrelated startup failure. Anything else
 * (network timeouts, version mismatches, ...) returns null and falls through
 * to the generic retry text — deliberately narrow scope.
 */
export function classifyBackendFailure(text) {
  const t = String(text || "");
  if (/claude` CLI was not found|coding backend unavailable/i.test(t)) {
    return "cli-missing";
  }
  // Matched on the two genuinely-missing-token AuthError texts only
  // (config.py: "No subscription token found", "auth profile '<p>' has no
  // token"). Other AuthErrors (e.g. a stray ANTHROPIC_API_KEY) print a
  // "claude setup-token" Fix block too, but setup-token is the WRONG
  // remediation for them — they fall through to generic + verbatim detail.
  if (/No subscription token found|has no token\. Expected/i.test(t)) {
    return "not-logged-in";
  }
  return null;
}

/**
 * Attach to a running server, or spawn `nh start --no-open` and wait for it.
 * Returns {status:"attached"} | {status:"spawned", child} | {status:"failed",
 * reason, detail?}. The caller must retain `child`: it is the ONLY legitimate
 * kill target — the pidfile belongs to the operator and is never consulted
 * for killing (design rule from the plan; an attached server is not ours).
 */
export async function ensureServer({
  origin = DEFAULT_ORIGIN,
  spawnTimeoutMs = 20000,
  env = process.env,
  nhArgs = ["start", "--no-open"],
  fallbackPaths = DEFAULT_NH_PATHS,
  bundled = bundledNhPath(),
  // Called the instant a child exists. The caller must register it HERE: for
  // the ~20s until this function returns, a server booted with the old token
  // was in no registry at all, so nothing could stop it and a token save
  // reported "Connected" over it.
  onSpawn = () => {},
} = {}) {
  if ((await probe(origin)) === "up") return { status: "attached" };
  const bin = await resolveNhBin(env, fallbackPaths, bundled);
  if (!bin) {
    return { status: "failed", reason: "nh-not-found" };
  }
  // Merge over the process env: nh (and a shebang'd test double) needs
  // PATH/HOME; the env param carries only resolution overrides.
  // detached: its own process GROUP, so stopServer can take the workers down
  // with it. A plain SIGTERM to the direct child left `nh`'s spawned workers
  // reparented to init, still holding the port.
  // PATH is widened AFTER the merge so a caller-supplied env cannot silently
  // drop it: without this the Agent SDK cannot find `claude` and every task
  // fails, even though the board serves normally.
  const spawnEnv = { ...process.env, ...env };
  spawnEnv.PATH = mergePath(spawnEnv.PATH);
  // stdout/stderr are PIPED (not ignored) so a launch failure's own diagnosis
  // — e.g. `_assert_backend_usable`'s "claude CLI was not found" — can reach
  // error.html instead of a generic lifecycle reason. Both streams are
  // unref'd immediately: this child is detached and may keep running long
  // after ensureServer returns (or outlive this process entirely under
  // `node --test`), and a referenced pipe would otherwise keep the event loop
  // alive waiting on it.
  const capture = makeOutputCapture();
  const child = spawn(bin, nhArgs, {
    env: spawnEnv, detached: true, stdio: ["ignore", "pipe", "pipe"],
  });
  child.unref();
  for (const stream of [child.stdout, child.stderr]) {
    if (!stream) continue;
    stream.on("data", (chunk) => capture.add(chunk));
    // A stray stream-level error must not crash this process — an unhandled
    // 'error' event throws by default, and these streams now stay attached
    // for the server's whole lifetime (see capture.stop() below).
    stream.on("error", () => {});
    stream.unref?.();
  }
  // A throwing callback must not reject ensureServer with the child already
  // spawned and untracked.
  try { onSpawn(child); } catch { /* tracking must never break the spawn */ }
  const spawnErrored = new Promise((resolve) =>
    child.once("error", () => resolve("spawn-error")));
  // A backend-check refusal (`sys.exit(2)`) exits almost immediately — racing
  // its exit (not just the port) means Retry doesn't sit for the full
  // spawnTimeoutMs before showing the real reason. This races 'close', not
  // 'exit': 'exit' fires before the stdio pipes finish flushing, so a fast
  // non-zero exit could resolve the race before capture.text() has the
  // diagnosis. 'close' fires once both pipes have drained, guaranteeing the
  // captured output is complete before classifyBackendFailure runs below.
  const spawnExited = new Promise((resolve) =>
    child.once("close", (code, signal) => {
      if (code !== 0 || signal) resolve("spawn-exited");
    }));
  const raced = await Promise.race(
    [waitForServer(origin, spawnTimeoutMs), spawnErrored, spawnExited]);
  if (raced === "spawn-error" || (await probe(origin)) !== "up") {
    // NOT killed here: a slow-booting nh may still be about to win the port,
    // and killing it from inside ensureServer would race a legitimately
    // starting server. The child is returned instead, so the CALLER owns the
    // decision — main.mjs tracks it and stops it before starting a
    // replacement, which is what keeps Retry from accumulating servers.
    const text = capture.text();
    const cause = classifyBackendFailure(text);
    const reason = cause === "cli-missing" ? "backend-cli-missing"
      : cause === "not-logged-in" ? "backend-not-logged-in"
      : raced === "spawn-error" ? "spawn-error"
      : raced === "spawn-exited" ? "backend-exited"
      : "spawn-timeout";
    return { status: "failed", reason, detail: tailDetail(text), child };
  }
  // Confirmed up (SCRUM-49): capture is only needed for the failure window
  // now behind us, so stop retaining it — a long-lived spawned server would
  // otherwise leak its entire console output into this buffer forever.
  //
  // This must NOT stream.destroy() stdout/stderr. Doing so closes OUR read
  // end, which is the pipe's ONLY reader — the still-running server's very
  // next stdout/stderr write would then raise EPIPE immediately, which is
  // exactly the breakage this ticket exists to prevent, just triggered by us
  // instead of by a later Electron crash. The 'data' listeners registered
  // above stay attached and keep draining the pipe (so the server's writes
  // keep succeeding for as long as it runs); capture.stop() only makes them
  // discard bytes instead of buffering them.
  capture.stop();
  return { status: "spawned", child };
}

/**
 * SIGKILL the group. Used only after SIGTERM has been given time to work: a
 * server that ignores SIGTERM otherwise survives quit and keeps the port.
 */
export function forceStopServer(state) {
  return stopServer(state, "SIGKILL");
}

/**
 * Stop the server ONLY when this shell spawned it. Attached servers belong
 * to the operator — never touched, never pidfile-killed.
 *
 * NOTE: a `true` return means the signal was DELIVERED, not that the process
 * died. Callers must confirm death with hasExited().
 */
export function stopServer(state, signal = "SIGTERM") {
  // ownsChild is the single definition of "ours". Gating on status==="spawned"
  // here contradicted it: ensureServer returns {status:"failed", child} for a
  // slow-booting nh that may still bind the port, so this refused to stop a
  // server we started and left it reparented to init, holding the port.
  if (!ownsChild(state)) return false;
  const child = state.child;
  // Already gone: nothing to signal, and saying otherwise inflates the
  // "stopped" count. Crucially it also stops us reaching the group kill with a
  // PID the OS may have reused.
  if (hasExited(child)) return false;
  // Kill the process GROUP first: `nh` spawns workers, and signalling only the
  // direct child left them alive at PPID 1 holding the port.
  // Only signal a group while the child is demonstrably alive: after exit its
  // PID can be reused, and process.kill(-pid) would hit a stranger's group.
  if (canSignal(child)) {
    try {
      process.kill(-child.pid, signal);
      return true;
    } catch { /* no group (or already gone) — fall back to the child */ }
  }
  try {
    child.kill(signal);
    return true;
  } catch {
    return false;
  }
}
