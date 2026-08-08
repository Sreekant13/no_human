// Guards the embedded tray icon against the corruption class the E3 review
// caught (hand-typed base64 → invisible tray): decode, parse, inflate.
import assert from "node:assert/strict";
import test from "node:test";
import zlib from "node:zlib";

// main.mjs imports electron; read the constants from source text instead.
async function constantB64(name) {
  const fs = await import("node:fs");
  const src = fs.readFileSync(new URL("./main.mjs", import.meta.url), "utf8");
  const m = src.match(new RegExp(`${name} =\\s*((?:"[^"]*"\\s*\\+?\\s*)+);`));
  assert.ok(m, `${name} not found`);
  return m[1].match(/"([^"]*)"/g).map((q) => q.slice(1, -1)).join("");
}

/** Decode a 16x16 RGBA PNG to flat pixels, asserting its structure on the way. */
function decode16(raw) {
  assert.deepEqual([...raw.subarray(0, 8)],
                   [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  // IHDR: 16x16, 8-bit, RGBA
  assert.equal(raw.readUInt32BE(16), 16);
  assert.equal(raw.readUInt32BE(20), 16);
  // IDAT inflates to exactly 16 scanlines of (1 filter byte + 16*4 px)
  const idatLen = raw.readUInt32BE(33);
  const px = zlib.inflateSync(raw.subarray(41, 41 + idatLen));
  assert.equal(px.length, 16 * (16 * 4 + 1));
  // Reverse the per-scanline PNG filters so the COLOURS can be read, not just
  // the byte count. Without this the guard sees structure and not pigment,
  // which is how a black-on-black Windows tray icon passed review.
  const S = 16 * 4, out = Buffer.alloc(16 * S);
  for (let y = 0; y < 16; y++) {
    const ft = px[y * (S + 1)];
    const line = px.subarray(y * (S + 1) + 1, y * (S + 1) + 1 + S);
    for (let x = 0; x < S; x++) {
      const a = x >= 4 ? out[y * S + x - 4] : 0;
      const b = y > 0 ? out[(y - 1) * S + x] : 0;
      const c = (x >= 4 && y > 0) ? out[(y - 1) * S + x - 4] : 0;
      let v = line[x];
      if (ft === 1) v += a;
      else if (ft === 2) v += b;
      else if (ft === 3) v += (a + b) >> 1;
      else if (ft === 4) {
        const p = a + b - c;
        const pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
        v += (pa <= pb && pa <= pc) ? a : (pb <= pc ? b : c);
      }
      out[y * S + x] = v & 0xff;
    }
  }
  return out;
}

// WCAG relative luminance / contrast ratio — the same arithmetic the finding
// was measured with, so the numbers in main.mjs's comment are reproducible.
const chan = (c) => {
  const s = c / 255;
  return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
};
const lum = (r, g, b) => 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b);
const contrast = (a, b) => (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);

/** How many FULLY OPAQUE pixels clear *min* contrast against a background. */
function legiblePixels(px, [br, bg, bb], min) {
  const lbg = lum(br, bg, bb);
  let n = 0;
  for (let i = 0; i < px.length; i += 4) {
    if (px[i + 3] < 250) continue;                 // antialiasing doesn't count
    if (contrast(lum(px[i], px[i + 1], px[i + 2]), lbg) >= min) n++;
  }
  return n;
}

const WIN_DARK_TASKBAR = [0x20, 0x20, 0x20];
const WIN_LIGHT_TASKBAR = [0xf3, 0xf3, 0xf3];

test("TRAY_ICON_B64 is a valid, complete 16x16 RGBA PNG", async () => {
  decode16(Buffer.from(await constantB64("TRAY_ICON_B64"), "base64"));
});

test("TRAY_ICON_B64 is a macOS TEMPLATE mask — alpha only, no colour", async () => {
  const px = decode16(Buffer.from(await constantB64("TRAY_ICON_B64"), "base64"));
  // macOS repaints a template from its alpha, so the RGB must stay black. This
  // also pins WHY it may not be reused on Windows, which renders the RGB.
  for (let i = 0; i < px.length; i += 4) {
    assert.ok(px[i] === 0 && px[i + 1] === 0 && px[i + 2] === 0,
      `template mask must be pure black; found ${px[i]},${px[i + 1]},${px[i + 2]}`);
  }
});

test("TRAY_ICON_WIN_B64 is a valid, complete 16x16 RGBA PNG", async () => {
  decode16(Buffer.from(await constantB64("TRAY_ICON_WIN_B64"), "base64"));
});

// THE REGRESSION GUARD. Windows ignores setTemplateImage and paints the PNG's
// own RGB, so the tray glyph must carry its own contrast. Measured when this
// landed: 68 opaque px >= 4.5:1 on the dark taskbar, 44 on the light one. The
// macOS mask scores ZERO on dark (1.29:1 everywhere) and fails this outright.
test("TRAY_ICON_WIN_B64 is legible on BOTH Windows taskbar themes", async () => {
  const px = decode16(Buffer.from(await constantB64("TRAY_ICON_WIN_B64"), "base64"));
  const onDark = legiblePixels(px, WIN_DARK_TASKBAR, 4.5);
  const onLight = legiblePixels(px, WIN_LIGHT_TASKBAR, 4.5);
  assert.ok(onDark >= 24,
    `only ${onDark} opaque px reach 4.5:1 on the dark taskbar (#202020) — a `
    + "near-black glyph there is invisible, and the tray menu is the only "
    + "reachable Quit on Windows");
  assert.ok(onLight >= 24,
    `only ${onLight} opaque px reach 4.5:1 on the light taskbar (#F3F3F3) — `
    + "inverting the glyph to pure white just moves the defect to light mode");
});

test("the two tray glyphs are distinct — the mask must not ship on Windows", async () => {
  assert.notEqual(await constantB64("TRAY_ICON_B64"),
                  await constantB64("TRAY_ICON_WIN_B64"));
});
