// WHICH tray bitmap each platform actually gets — the routing, not the asset.
//
// THE GAP THIS CLOSES. trayIcon.test.mjs pins both bitmaps hard: it decodes,
// inflates and measures them, proving the macOS mask is pure black and the
// win32 glyph clears 4.5:1 on both taskbar themes. But it reads the two
// constants out of main.mjs as SOURCE TEXT, because main.mjs imports electron.
// Nothing anywhere called trayIcon(). So deleting its three-line win32 dispatch
//     if (process.platform === "win32") {
//       return nativeImage.createFromBuffer(Buffer.from(TRAY_ICON_WIN_B64, "base64"));
//     }
// left all 281 tests green while shipping the macOS TEMPLATE MASK on Windows —
// which is the original blocker, not a cosmetic one. setTemplateImage is a
// no-op off macOS, so the mask renders as its own black RGB: measured 1.29:1
// against the Windows dark taskbar (#202020), with zero opaque pixels reaching
// even 3:1. On Windows `close` hides to the tray and docs/WINDOWS.md §7 records
// that the menu bar is unreachable there, so the tray menu is the only
// reachable Quit — an invisible icon strands the user.
//
// The asset was guarded and the routing was not, so this file asserts the
// routing: BOTH directions, because a test that only proved win32 gets the
// glyph would also pass if every platform got it, and that would put a
// full-colour icon in the macOS menu bar where the mask is correct.
import { register } from "node:module";
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

register("./testing/electronLoader.mjs", import.meta.url);

// A private HOME before main.mjs is imported: nothing here may read or write
// the operator's real ~/.no_human.
const home = fs.mkdtempSync(path.join(os.tmpdir(), "nh-tray-home-"));
process.env.HOME = home;
process.env.USERPROFILE = home;

const stub = await import("./testing/electronStub.mjs");
// A userData dir of this file's own, so no persisted state is shared with the
// other desktop test files.
const userData = fs.mkdtempSync(path.join(os.tmpdir(), "nh-tray-data-"));
stub.app.getPath = () => userData;

// main.mjs is imported but never made ready: trayIcon() reads process.platform
// at CALL time, so one process can ask it both questions. app.whenReady() is
// left unresolved deliberately — no window, no server probe, no tray.
const { trayIcon, TRAY_ICON_B64, TRAY_ICON_WIN_B64 } = await import("./main.mjs");

test.after(() => {
  fs.rmSync(home, { recursive: true, force: true });
  fs.rmSync(userData, { recursive: true, force: true });
});

const REAL_PLATFORM_DESC = Object.getOwnPropertyDescriptor(process, "platform");

/** Call *fn* with process.platform reading as *name*, then restore it. */
function onPlatform(name, fn) {
  Object.defineProperty(process, "platform", { value: name, configurable: true });
  try {
    return fn();
  } finally {
    Object.defineProperty(process, "platform", REAL_PLATFORM_DESC);
  }
}

const MASK = Buffer.from(TRAY_ICON_B64, "base64");
const WIN_GLYPH = Buffer.from(TRAY_ICON_WIN_B64, "base64");

test("the two bitmaps are actually different — or everything below is vacuous",
  () => {
    // Without this, a copy-paste that pointed both constants at the same base64
    // would leave both routing assertions passing while the defect was shipped.
    assert.ok(MASK.length > 0 && WIN_GLYPH.length > 0);
    assert.ok(!MASK.equals(WIN_GLYPH),
      "the macOS mask and the win32 glyph are byte-identical, so no assertion "
      + "in this file can tell the two routes apart");
  });

test("win32 gets the real-colour glyph, NOT the template mask", () => {
  const img = onPlatform("win32", () => trayIcon());

  assert.ok(img.buffer.equals(WIN_GLYPH),
    "Windows was handed the macOS TEMPLATE MASK. setTemplateImage is a no-op "
    + "off macOS, so the mask paints as its own black RGB: 1.29:1 on the "
    + "#202020 dark taskbar, with zero opaque pixels even at 3:1. The tray menu "
    + "is the only reachable Quit on Windows, so this strands the user");
  assert.equal(img.templated, false,
    "setTemplateImage was called on the Windows icon. It does nothing off "
    + "macOS, so this cannot help — but it is the fingerprint of the mask "
    + "path having been taken, which means the wrong bitmap shipped");
});

test("darwin still gets the template mask, which is correct there", () => {
  const img = onPlatform("darwin", () => trayIcon());

  assert.ok(img.buffer.equals(MASK),
    "macOS was handed the full-colour Windows glyph. The menu bar wants a "
    + "template mask so the OS can recolour it for light/dark and for the "
    + "highlighted state; a real-colour bitmap ignores all three");
  assert.equal(img.templated, true,
    "the macOS icon must be marked a template image, or the menu bar paints "
    + "its literal black pixels and it disappears in dark mode");
});

test("the platform routing is read per call, not frozen at module load", () => {
  // The property that makes the two tests above trustworthy: if trayIcon()
  // cached the platform, whichever ran first would decide both answers.
  const a = onPlatform("win32", () => trayIcon());
  const b = onPlatform("darwin", () => trayIcon());
  const c = onPlatform("win32", () => trayIcon());
  assert.ok(a.buffer.equals(WIN_GLYPH));
  assert.ok(b.buffer.equals(MASK));
  assert.ok(c.buffer.equals(WIN_GLYPH),
    "asking for win32 a second time gave a different answer, so something is "
    + "caching the platform and these assertions depend on their own order");
});

test("no test above left the platform flipped", () => {
  assert.deepEqual(Object.getOwnPropertyDescriptor(process, "platform"),
    REAL_PLATFORM_DESC, "process.platform was left redefined");
});
