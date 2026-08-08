// Dock/taskbar badge = the needs-you count. The web app already encodes it in
// the window title ("(N) no_human", notifications.js titleWithBadge) — the
// shell parses that instead of re-deriving needs-you, so the badge can never
// drift from the board (same single-source rule as isNeedsYou).
//
// Contract: a title in the app's own format returns its count (0 when clean);
// any OTHER title (the error page, a mid-navigation document title) returns
// null = "no information" — the shell keeps the last truthful badge instead
// of wiping a count that is still true (PR #104 review, low).
export function parseBadgeCount(title) {
  const t = title || "";
  if (t === "no_human") return 0;
  const m = /^\((\d+)\) no_human$/.exec(t);
  return m ? Number(m[1]) : null;
}

// ------------------------- Windows overlay badge ------------------------- //
//
// macOS renders the dock badge itself from app.setBadgeCount; Windows has no
// dock badge — the taskbar equivalent is win.setOverlayIcon(nativeImage), and
// Electron will not draw the number for us. This rasterizes it: a red disc
// with the count in white, as raw BGRA bytes that main.mjs turns into a
// nativeImage. Pure math on a Uint8Array — no electron import, so it stays
// unit-testable with `node --test` like the parser above (same reason
// menu.mjs is pure).
//
// 32×32 px handed over with scaleFactor 2.0 = 16×16 DIP, the documented
// overlay size, crisp on a 200% display and downscaled by the OS elsewhere.
//
// A 3×5 bitmap font, digits only, one number per glyph read row-wise from the
// low bit. 15 bits fit in a number literal; a wrong bit shows up in the
// rendered-pixel tests below rather than on an operator's taskbar.
const GLYPHS = {
  1: 0b010110010010111, 2: 0b110001010100111, 3: 0b110001110001110,
  4: 0b101101111001001, 5: 0b111100110001110, 6: 0b011100111101111,
  7: 0b111001010100100, 8: 0b111101010101111, 9: 0b111101111001110,
  "+": 0b000010111010000,
};

function drawGlyph(px, size, glyph, x0, y0, scale) {
  for (let row = 0; row < 5; row++) {
    for (let col = 0; col < 3; col++) {
      if (!((GLYPHS[glyph] >> (14 - (row * 3 + col))) & 1)) continue;
      for (let dy = 0; dy < scale; dy++) {
        for (let dx = 0; dx < scale; dx++) {
          const x = x0 + col * scale + dx, y = y0 + row * scale + dy;
          const i = (y * size + x) * 4;
          px[i] = 0xFF; px[i + 1] = 0xFF; px[i + 2] = 0xFF; px[i + 3] = 0xFF;
        }
      }
    }
  }
}

// count >= 1 (the caller clears the overlay for 0 — an all-transparent image
// would still occupy the overlay slot). Returns {width, height, data: BGRA}.
export function overlayBadgeBitmap(count) {
  const size = 32;
  const px = new Uint8Array(size * size * 4);
  // Red disc, anti-aliased at the rim by alpha. BGRA: #D93025 -> 25 30 D9.
  const c = (size - 1) / 2, r = size / 2 - 1;
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const d = Math.sqrt((x - c) ** 2 + (y - c) ** 2);
      if (d > r + 0.5) continue;
      const a = d < r - 0.5 ? 1 : r + 0.5 - d;
      const i = (y * size + x) * 4;
      px[i] = 0x25; px[i + 1] = 0x30; px[i + 2] = 0xD9;
      px[i + 3] = Math.round(a * 0xFF);
    }
  }
  if (count <= 9) {
    // one glyph, 3×5 at scale 3 = 9×15, centered
    drawGlyph(px, size, count, Math.floor((size - 9) / 2), Math.floor((size - 15) / 2), 3);
  } else {
    // "9+" — the exact count still reaches the operator via the overlay's
    // accessibility description and the window title
    drawGlyph(px, size, 9, 8, 11, 2);   // 6×10 at (8,11)
    drawGlyph(px, size, "+", 16, 11, 2); // 6×10 at (16,11), pair centered on x=15

  }
  return { width: size, height: size, data: px };
}
