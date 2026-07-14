// Guards the embedded tray icon against the corruption class the E3 review
// caught (hand-typed base64 → invisible tray): decode, parse, inflate.
import assert from "node:assert/strict";
import test from "node:test";
import zlib from "node:zlib";

test("TRAY_ICON_B64 is a valid, complete 16x16 RGBA PNG", async () => {
  // main.mjs imports electron; read the constant from source text instead.
  const fs = await import("node:fs");
  const src = fs.readFileSync(new URL("./main.mjs", import.meta.url), "utf8");
  const m = src.match(/TRAY_ICON_B64 =\s*((?:"[^"]*"\s*\+?\s*)+);/);
  assert.ok(m, "TRAY_ICON_B64 not found");
  const b64 = m[1].match(/"([^"]*)"/g).map((q) => q.slice(1, -1)).join("");
  const raw = Buffer.from(b64, "base64");
  assert.deepEqual([...raw.subarray(0, 8)],
                   [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  // IHDR: 16x16, 8-bit, RGBA
  assert.equal(raw.readUInt32BE(16), 16);
  assert.equal(raw.readUInt32BE(20), 16);
  // IDAT inflates to exactly 16 scanlines of (1 filter byte + 16*4 px)
  const idatLen = raw.readUInt32BE(33);
  const idat = raw.subarray(41, 41 + idatLen);
  const px = zlib.inflateSync(idat);
  assert.equal(px.length, 16 * (16 * 4 + 1));
});
