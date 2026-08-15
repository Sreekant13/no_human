#!/usr/bin/env node
// Derive desktop/build/icon.icns and desktop/build/icon.ico from the single
// shipped brand master, at package time — never committed.
//
// WHY THIS EXISTS. The two icon binaries used to be committed. The mark they
// were cut from (dropped 2026-08-15) compresses to a byte stream whose
// coincidental 3-byte substrings trip the identity scanner's needle terms —
// a structural false positive the scanner cannot tell apart from a real
// leak, verified byte-level. Rather than weaken the scanner (never do that),
// the binaries simply stop existing as tracked files: this script derives
// them from web/public/nh-mark-512.png (scanned clean) on every build, so
// there is exactly one source of truth and nothing for the scanner to trip
// on. Run it directly, or via `npm run dist*` in desktop/, which prepend it.
//
// 512-MAX ICONSET, DOCUMENTED. The brand master ships at 512x512 only. A
// full macOS iconset also wants a 1024x1024 variant (icon_512x512@2x.png,
// OSType ic10) for Finder's largest preview. Upscaling 512 -> 1024 was
// considered and rejected: it cannot add real detail, only interpolation
// artifacts, and a soft "1024" is worse than a crisp 512 that macOS itself
// upscales on demand. So this script emits the nine iconset files a 512px
// master can honestly produce (16/32/32/64/128/256/256/512, i.e. up to
// icon_256x256@2x.png = 512x512) and no ic10. When a 1024px master lands,
// the fix is one line: add ["icon_512x512@2x.png", 1024] to ICONSET_SPECS
// below.
//
// FAIL-CLOSED. Every failure mode below prints one `FAIL:` line naming the
// actual cause and exits non-zero: no silent partial output, ever. A build
// that cannot produce a real icon must not produce a fake one.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

import { buildIco, validateIco } from "./icoFromPng.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const DEFAULT_MASTER = path.join(ROOT, "web/public/nh-mark-512.png");
const DEFAULT_OUT_DIR = path.join(ROOT, "desktop/build");

// icon.iconset filenames macOS expects, and the pixel size sips emits at
// each. A 512px master maxes out here — see the header comment above.
const ICONSET_SPECS = [
  ["icon_16x16.png", 16],
  ["icon_16x16@2x.png", 32],
  ["icon_32x32.png", 32],
  ["icon_32x32@2x.png", 64],
  ["icon_128x128.png", 128],
  ["icon_128x128@2x.png", 256],
  ["icon_256x256.png", 256],
  ["icon_256x256@2x.png", 512],
  ["icon_512x512.png", 512],
];

function fail(msg) {
  console.error(`FAIL: ${msg}`);
  process.exit(1);
}

function parseArgs(argv) {
  const args = { verify: false, requireIcns: false, outDir: DEFAULT_OUT_DIR };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--verify") args.verify = true;
    else if (a === "--require-icns") args.requireIcns = true;
    else if (a === "--out-dir") args.outDir = path.resolve(argv[++i] ?? fail("--out-dir needs a value"));
    else fail(`unrecognized argument: ${a}`);
  }
  return args;
}

function resolveMasterPath() {
  // NH_ICON_MASTER is a test-only override — production callers always get
  // the real shipped mark. Documented here, not just in the plan, so a
  // future reader of this file sees the same thing.
  return process.env.NH_ICON_MASTER ? path.resolve(process.env.NH_ICON_MASTER) : DEFAULT_MASTER;
}

function readAndDecodeMaster(masterPath) {
  if (!fs.existsSync(masterPath)) {
    fail(`no master PNG at ${masterPath} — the desktop icons cannot be derived without it`);
  }
  let buf;
  try {
    buf = fs.readFileSync(masterPath);
  } catch (e) {
    fail(`cannot read master PNG at ${masterPath}: ${e.message}`);
  }
  return buf;
}

function deriveIco(masterBuf, outDir) {
  let icoBuf;
  try {
    icoBuf = buildIco(masterBuf);
    validateIco(icoBuf);
  } catch (e) {
    fail(`ico derivation from the master PNG failed: ${e.message}`);
  }
  const icoPath = path.join(outDir, "icon.ico");
  try {
    fs.writeFileSync(icoPath, icoBuf);
  } catch (e) {
    fail(`could not write ${icoPath}: ${e.message}`);
  }
  try {
    validateIco(fs.readFileSync(icoPath));
  } catch (e) {
    fail(`icon.ico written to ${icoPath} failed re-validation from disk: ${e.message}`);
  }
  console.log(`OK: wrote ${icoPath}`);
}

function runTool(cmd, cmdArgs) {
  const r = spawnSync(cmd, cmdArgs, { stdio: ["ignore", "pipe", "pipe"] });
  if (r.error) {
    fail(`${cmd} is not available: ${r.error.message}`);
  }
  if (r.status !== 0) {
    fail(`${cmd} ${cmdArgs.join(" ")} failed (exit ${r.status}): ${(r.stderr || r.stdout || "").toString().trim()}`);
  }
  return r;
}

function deriveIcns(masterPath, outDir) {
  const tmpBase = fs.mkdtempSync(path.join(os.tmpdir(), "nh-icon-"));
  try {
    const iconsetDir = path.join(tmpBase, "icon.iconset");
    fs.mkdirSync(iconsetDir);
    for (const [name, size] of ICONSET_SPECS) {
      const out = path.join(iconsetDir, name);
      runTool("sips", ["-z", String(size), String(size), masterPath, "--out", out]);
    }
    const icnsPath = path.join(outDir, "icon.icns");
    runTool("iconutil", ["-c", "icns", iconsetDir, "-o", icnsPath]);
    console.log(`OK: wrote ${icnsPath}`);
  } finally {
    fs.rmSync(tmpBase, { recursive: true, force: true });
  }
}

function derive({ outDir, requireIcns }) {
  const masterPath = resolveMasterPath();
  const masterBuf = readAndDecodeMaster(masterPath);

  fs.mkdirSync(outDir, { recursive: true });

  deriveIco(masterBuf, outDir);

  if (process.platform !== "darwin") {
    if (requireIcns) {
      fail(`icns derivation needs macOS (sips/iconutil); --require-icns was set on ${process.platform}`);
    }
    console.log(`SKIP: icns derivation needs macOS (sips/iconutil) — skipped on ${process.platform}`);
    return;
  }
  deriveIcns(masterPath, outDir);
}

// --- .icns TOC walk, shared by verify() below --- //
function icnsOSTypes(buf) {
  if (buf.length < 8 || buf.toString("latin1", 0, 4) !== "icns") {
    throw new Error("not an icns file (bad magic)");
  }
  const total = buf.readUInt32BE(4);
  const present = [];
  let off = 8;
  while (off + 8 <= Math.min(total, buf.length)) {
    const type = buf.toString("ascii", off, off + 4);
    const len = buf.readUInt32BE(off + 4);
    if (len < 8) break;
    present.push(type);
    off += len;
  }
  return present;
}

function verify({ outDir, requireIcns }) {
  const masterPath = resolveMasterPath();
  if (!fs.existsSync(masterPath)) {
    fail(`--verify: no master PNG at ${masterPath}`);
  }
  const masterMtime = fs.statSync(masterPath).mtimeMs;

  const icoPath = path.join(outDir, "icon.ico");
  if (!fs.existsSync(icoPath)) {
    fail(`--verify: no derived ico at ${icoPath} — run derive-icons.mjs first`);
  }
  if (fs.statSync(icoPath).mtimeMs < masterMtime) {
    fail(`--verify: ${icoPath} is older than the master (${masterPath}) — stale, re-run derive-icons.mjs`);
  }
  try {
    validateIco(fs.readFileSync(icoPath));
  } catch (e) {
    fail(`--verify: ${icoPath} failed validation: ${e.message}`);
  }

  const wantIcns = process.platform === "darwin" || requireIcns;
  if (!wantIcns) {
    console.log("OK: derived icons verified (ico only — icns needs macOS)");
    return;
  }

  const icnsPath = path.join(outDir, "icon.icns");
  if (!fs.existsSync(icnsPath)) {
    fail(`--verify: no derived icns at ${icnsPath} — run derive-icons.mjs first`);
  }
  if (fs.statSync(icnsPath).mtimeMs < masterMtime) {
    fail(`--verify: ${icnsPath} is older than the master (${masterPath}) — stale, re-run derive-icons.mjs`);
  }
  let osTypes;
  try {
    osTypes = icnsOSTypes(fs.readFileSync(icnsPath));
  } catch (e) {
    fail(`--verify: ${icnsPath} failed validation: ${e.message}`);
  }
  if (osTypes.length === 0) {
    fail(`--verify: ${icnsPath} has an empty TOC`);
  }

  if (process.platform === "darwin") {
    const tmpBase = fs.mkdtempSync(path.join(os.tmpdir(), "nh-icon-verify-"));
    try {
      const iconsetOut = path.join(tmpBase, "roundtrip.iconset");
      runTool("iconutil", ["-c", "iconset", icnsPath, "-o", iconsetOut]);
      const expected = ICONSET_SPECS.map(([name]) => name).sort();
      const got = fs.readdirSync(iconsetOut).sort();
      if (JSON.stringify(got) !== JSON.stringify(expected)) {
        fail(
          `--verify: iconutil roundtrip of ${icnsPath} produced ${JSON.stringify(got)}, want ${JSON.stringify(expected)}`,
        );
      }
    } finally {
      fs.rmSync(tmpBase, { recursive: true, force: true });
    }
  } else if (requireIcns) {
    fail("--verify --require-icns on a non-darwin platform cannot roundtrip icns (iconutil is unavailable)");
  }

  console.log("OK: derived icons verified");
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.verify) {
    verify(args);
  } else {
    derive(args);
  }
}

main();
