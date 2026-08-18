// Tests for packaging/derive-icons.mjs and packaging/icoFromPng.mjs — the
// pipeline that replaces the two committed icon binaries (desktop/build/
// icon.icns, icon.ico) with icons derived at package time from the single
// shipped brand master, web/public/nh-mark-512.png. See derive-icons.mjs's
// header comment for WHY: the old binaries' byte stream coincidentally
// tripped the identity scanner's needle terms, and the fix is to stop
// committing the binaries at all rather than weaken the scanner.
//
// Every test here derives into its own fs.mkdtempSync() output dir (via
// --out-dir) and, where the master matters, its own copy of the real master
// (via NH_ICON_MASTER) — never desktop/build. That directory is exercised by
// packagedFiles.test.mjs instead, which needs the real derived output on disk
// for electron-builder.config.cjs's own freshness check to pass at import
// time.
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import zlib from "node:zlib";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { buildIco, validateIco } from "../packaging/icoFromPng.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(here, "..");
const DERIVE_SCRIPT = path.join(ROOT, "packaging", "derive-icons.mjs");
const REAL_MASTER = path.join(ROOT, "web", "public", "nh-mark-512.png");
const MAKE_DMG = path.join(ROOT, "packaging", "make-dmg.sh");

function tmpDir(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

function runDerive(args, env) {
  return spawnSync(process.execPath, [DERIVE_SCRIPT, ...args], {
    encoding: "utf8",
    env: { ...process.env, ...env },
  });
}

// --- CRC-32, duplicated from icoFromPng.mjs on purpose: this file must be
// able to hand-build a PNG icoFromPng.mjs is NOT supposed to understand
// (test #6), so it cannot borrow icoFromPng.mjs's own encoder for that. --- //
const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();
function crc32(buf) {
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}
function pngChunk(type, data) {
  const typeBuf = Buffer.from(type, "ascii");
  const lenBuf = Buffer.alloc(4);
  lenBuf.writeUInt32BE(data.length, 0);
  const crcBuf = Buffer.alloc(4);
  crcBuf.writeUInt32BE(crc32(Buffer.concat([typeBuf, data])), 0);
  return Buffer.concat([lenBuf, typeBuf, data, crcBuf]);
}
// A minimal, valid, 2x2 GRAYSCALE (colorType 0) PNG — a shape decodePng()
// explicitly refuses (it only understands 8-bit RGBA, non-interlaced).
function buildGrayscalePng() {
  const sig = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(2, 0); // width
  ihdr.writeUInt32BE(2, 4); // height
  ihdr[8] = 8; // bit depth
  ihdr[9] = 0; // colour type: grayscale
  ihdr[10] = 0;
  ihdr[11] = 0;
  ihdr[12] = 0; // interlace: none
  const raw = Buffer.from([0, 0, 0, 0, 0, 0, 0]); // 2 rows, filter-None + 2 gray bytes each
  const idat = zlib.deflateSync(raw);
  return Buffer.concat([sig, pngChunk("IHDR", ihdr), pngChunk("IDAT", idat), pngChunk("IEND", Buffer.alloc(0))]);
}

test("derives an ico with exactly 6 PNG entries, first payload at offset 102", () => {
  const masterBuf = fs.readFileSync(REAL_MASTER);
  const ico = buildIco(masterBuf);
  assert.doesNotThrow(() => validateIco(ico));
  assert.equal(ico.readUInt16LE(4), 6, "ico must declare exactly 6 entries");
  assert.equal(ico.readUInt32LE(6 + 12), 102, "first payload must start at offset 102");
});

test("the derived ico is byte-identical across runs", () => {
  const masterBuf = fs.readFileSync(REAL_MASTER);
  const a = buildIco(masterBuf);
  const b = buildIco(masterBuf);
  assert.ok(a.equals(b), "buildIco() must be a pure function of its input — no timestamps, no randomness");
});

test("icns roundtrips through iconutil", { skip: process.platform !== "darwin" && "icns derivation needs macOS (sips/iconutil)" }, () => {
  const outDir = tmpDir("nh-icon-roundtrip-");
  const r = runDerive(["--out-dir", outDir, "--require-icns"], { NH_ICON_MASTER: REAL_MASTER });
  assert.equal(r.status, 0, `derive-icons.mjs failed: ${r.stderr}`);
  const icnsPath = path.join(outDir, "icon.icns");
  assert.ok(fs.existsSync(icnsPath));

  const iconsetOut = path.join(tmpDir("nh-icon-roundtrip-out-"), "roundtrip.iconset");
  const rt = spawnSync("iconutil", ["-c", "iconset", icnsPath, "-o", iconsetOut], { encoding: "utf8" });
  assert.equal(rt.status, 0, `iconutil roundtrip failed: ${rt.stderr}`);
  assert.ok(fs.existsSync(iconsetOut) && fs.statSync(iconsetOut).isDirectory());
});

test("the icns carries every variant a 512px master can produce, and no ic10", { skip: process.platform !== "darwin" && "icns derivation needs macOS (sips/iconutil)" }, () => {
  const outDir = tmpDir("nh-icon-toc-");
  const r = runDerive(["--out-dir", outDir, "--require-icns"], { NH_ICON_MASTER: REAL_MASTER });
  assert.equal(r.status, 0, `derive-icons.mjs failed: ${r.stderr}`);
  const buf = fs.readFileSync(path.join(outDir, "icon.icns"));
  assert.equal(buf.subarray(0, 4).toString("latin1"), "icns");
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
  // Empirically confirmed (this task, live sips/iconutil run against the real
  // master): a 512px master's 9-file iconset produces exactly this OSType set
  // plus one `info` plist chunk, and no ic10 (that needs a 1024px source).
  const EXPECTED_IMAGE_TYPES = ["ic04", "ic05", "ic07", "ic08", "ic09", "ic11", "ic12", "ic13", "ic14"];
  for (const t of EXPECTED_IMAGE_TYPES) {
    assert.ok(present.includes(t), `icns TOC missing ${t}: ${JSON.stringify(present)}`);
  }
  assert.ok(!present.includes("ic10"),
    "icns must not carry ic10 (1024px) — the 512px master cannot honestly produce it; upscaling was rejected, see derive-icons.mjs's header comment");
});

test("derivation refuses loudly when the master is missing", () => {
  const outDir = tmpDir("nh-icon-missing-");
  const missingMaster = path.join(outDir, "does-not-exist.png");
  const r = runDerive(["--out-dir", path.join(outDir, "out")], { NH_ICON_MASTER: missingMaster });
  assert.notEqual(r.status, 0, "derivation must fail when the master PNG is absent");
  assert.match(r.stderr, /FAIL:/);
  assert.match(r.stderr, new RegExp(missingMaster.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")),
    "the failure must name the actual missing path");
  assert.ok(!fs.existsSync(path.join(outDir, "out")),
    "a missing master must produce NO output — not even the output directory");
});

test("derivation refuses a master that is not 8-bit RGBA non-interlaced", () => {
  const outDir = tmpDir("nh-icon-badshape-");
  const masterPath = path.join(outDir, "grayscale-master.png");
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(masterPath, buildGrayscalePng());
  const r = runDerive(["--out-dir", path.join(outDir, "out")], { NH_ICON_MASTER: masterPath });
  assert.notEqual(r.status, 0, "derivation must refuse a non-RGBA master");
  assert.match(r.stderr, /FAIL:/);
  assert.ok(!fs.existsSync(path.join(outDir, "out", "icon.ico")),
    "a rejected master must not leave a partial icon.ico behind");
});

test("--verify fails when a derived icon is stale", () => {
  const outDir = tmpDir("nh-icon-stale-");
  const masterPath = path.join(outDir, "master.png");
  fs.mkdirSync(outDir, { recursive: true });
  fs.copyFileSync(REAL_MASTER, masterPath);

  const derived = runDerive(["--out-dir", path.join(outDir, "out")], { NH_ICON_MASTER: masterPath });
  assert.equal(derived.status, 0, `initial derive failed: ${derived.stderr}`);
  const verifyFresh = runDerive(["--verify", "--out-dir", path.join(outDir, "out")], { NH_ICON_MASTER: masterPath });
  assert.equal(verifyFresh.status, 0, `--verify on a freshly derived icon must pass: ${verifyFresh.stderr}`);

  // Move the master's mtime into the future so it reads as newer than the
  // icon that was just derived from it.
  const future = new Date(Date.now() + 60_000);
  fs.utimesSync(masterPath, future, future);
  const verifyStale = runDerive(["--verify", "--out-dir", path.join(outDir, "out")], { NH_ICON_MASTER: masterPath });
  assert.notEqual(verifyStale.status, 0, "--verify must fail once the master is newer than the derived icon");
  assert.match(verifyStale.stderr, /FAIL:/);
  assert.match(verifyStale.stderr, /stale/);
});

test("make-dmg.sh gates on derivation before it builds the image", () => {
  const sh = fs.readFileSync(MAKE_DMG, "utf8");
  // Line-anchored, not a bare substring search: the file's own header comment
  // mentions both "derive-icons.mjs" and "hdiutil" in prose before either is
  // actually invoked, which would make a naive indexOf() ordering check pass
  // on the comment text alone rather than the real commands.
  const deriveMatch = sh.match(/^\s*node\s+"[^"]*derive-icons\.mjs"/m);
  assert.ok(deriveMatch, "make-dmg.sh no longer invokes derive-icons.mjs at all");
  const hdiutilMatch = sh.match(/^hdiutil\s/m);
  assert.ok(hdiutilMatch, "make-dmg.sh no longer calls hdiutil - has the DMG build moved?");
  assert.ok(deriveMatch.index < hdiutilMatch.index,
    "derive-icons.mjs must run before the first hdiutil call, or make-dmg.sh can package a stale/missing icon");
  assert.match(sh, /derive-icons\.mjs"[^\n]*\n[^\n]*\n\s*node\s+"[^"]*derive-icons\.mjs"\s+--verify/,
    "make-dmg.sh must call derive-icons.mjs and then --verify its own output, not just derive and hope");
});

test("electron-builder config refuses when the derived ico is absent", () => {
  const outDir = tmpDir("nh-icon-cfgcheck-");
  fs.mkdirSync(path.join(outDir, "desktop", "build"), { recursive: true });
  fs.mkdirSync(path.join(outDir, "web", "public"), { recursive: true });
  fs.copyFileSync(REAL_MASTER, path.join(outDir, "web", "public", "nh-mark-512.png"));
  fs.cpSync(path.join(ROOT, "desktop"), path.join(outDir, "desktop"), { recursive: true });
  fs.cpSync(path.join(ROOT, "packaging"), path.join(outDir, "packaging"), { recursive: true });
  // The real desktop/build may itself hold freshly derived icons (other tests
  // in this suite, and packagedFiles.test.mjs, put them there) — strip the
  // copies so this scratch tree starts with NO icon.ico or icon.icns, which
  // is the actual condition under test.
  fs.rmSync(path.join(outDir, "desktop", "build", "icon.ico"), { force: true });
  fs.rmSync(path.join(outDir, "desktop", "build", "icon.icns"), { force: true });
  const r = spawnSync(process.execPath, ["-e", "require('./electron-builder.config.cjs')"], {
    cwd: path.join(outDir, "desktop"),
    encoding: "utf8",
  });
  assert.notEqual(r.status, 0, "electron-builder.config.cjs must refuse to load with no derived icon.ico present");
  assert.match(r.stderr, /derive-icons\.mjs/,
    "the refusal must point at derive-icons.mjs as the fix");
});

// --- Linux: desktop/build/icon.png, the third derived icon --- //
// electron-builder's Linux targets take ONE PNG >= 512x512 for linux.icon and
// render the hicolor size set themselves. The master already IS a 512x512 RGBA
// PNG, so the derivation is a byte-copy: never a re-encode, because every
// re-encode of this mark rolls fresh 3-byte coincidences against the identity
// scanner (the reason the icons stopped being committed at all).

test("derives icon.png for Linux as a byte-identical copy of the master", () => {
  const outDir = tmpDir("nh-icons-linux-");
  const r = runDerive(["--out-dir", outDir]);
  assert.equal(r.status, 0, r.stderr);
  const png = path.join(outDir, "icon.png");
  assert.ok(fs.existsSync(png), "icon.png (the Linux icon) was not derived");
  const buf = fs.readFileSync(png);
  assert.ok(buf.equals(fs.readFileSync(REAL_MASTER)),
    "icon.png must be the master's bytes — a re-encode would roll new "
    + "3-byte needle coincidences (the scanner-lottery class)");
  // PNG magic + IHDR width/height 512: electron-builder needs >= 512.
  assert.equal(buf.toString("hex", 0, 8), "89504e470d0a1a0a", "not a PNG");
  assert.equal(buf.readUInt32BE(16), 512, "master is not 512 wide");
  assert.equal(buf.readUInt32BE(20), 512, "master is not 512 high");
  assert.match(r.stdout, /OK: wrote .*icon\.png/);
});

test("--verify fails when icon.png is missing, stale, or not the master's bytes", () => {
  // Own copy of the master (NH_ICON_MASTER), like the ico staleness test above:
  // "stale" means OLDER THAN THE MASTER, so the master's mtime is the thing to
  // move — an absolute "one day ago" on the icon is not stale on a checkout
  // whose master file is older than that (it was, on the primary checkout, and
  // this test read green/red depending on when the master was last touched).
  const outDir = tmpDir("nh-icons-linux-");
  const masterPath = path.join(outDir, "master.png");
  fs.copyFileSync(REAL_MASTER, masterPath);
  const env = { NH_ICON_MASTER: masterPath };
  const out = path.join(outDir, "out");
  assert.equal(runDerive(["--out-dir", out], env).status, 0);
  const png = path.join(out, "icon.png");

  fs.unlinkSync(png);
  const missing = runDerive(["--verify", "--out-dir", out], env);
  assert.notEqual(missing.status, 0, "--verify passed with icon.png absent");
  assert.match(missing.stderr, /icon\.png/);

  assert.equal(runDerive(["--out-dir", out], env).status, 0);
  // Only the png goes stale (older than the master by a second) so the
  // refusal names icon.png — the ico is checked first and stays fresh.
  const masterMtime = fs.statSync(masterPath).mtime;
  const older = new Date(masterMtime.getTime() - 1000);
  fs.utimesSync(png, older, older);
  const stale = runDerive(["--verify", "--out-dir", out], env);
  assert.notEqual(stale.status, 0, "--verify passed with a stale icon.png");
  assert.match(stale.stderr, /icon\.png.*stale/);

  assert.equal(runDerive(["--out-dir", out], env).status, 0);   // re-derive: fresh again
  fs.writeFileSync(png, Buffer.concat([fs.readFileSync(png), Buffer.from([0])]));
  const tampered = runDerive(["--verify", "--out-dir", out], env);
  assert.notEqual(tampered.status, 0, "--verify passed with a modified icon.png");
  assert.match(tampered.stderr, /icon\.png.*master/);
});
