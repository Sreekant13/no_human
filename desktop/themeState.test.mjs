// themeState.mjs, on its own.
//
// The decisions here are cheap to get wrong and expensive to see: a normalize
// that admits anything is a board-content write primitive, a read that throws
// is a failed launch, and the two colour pairs are literals nothing else
// checks. mainTheme.test.mjs proves main.mjs WIRES these; this file proves the
// answers are right.
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  DEFAULT_THEME, THEME_FILE, normalizeTheme, readTheme, themeColors, writeTheme,
} from "./themeState.mjs";

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), "nh-themestate-"));

test("the default is DARK — the same default the board itself uses", () => {
  // web/src/App.jsx: localStorage.getItem("nh-theme") || "dark". If these two
  // ever disagree the shell pre-paints one theme and the board renders the
  // other, which is the exact defect this module exists to close.
  assert.equal(DEFAULT_THEME, "dark");
  const app = fs.readFileSync(new URL("../web/src/App.jsx", import.meta.url), "utf8");
  assert.match(app, /localStorage\.getItem\("nh-theme"\)\s*\|\|\s*"dark"/,
    "the board's own default is no longer dark — the shell now disagrees with it");
});

test("normalizeTheme admits the two themes and nothing else", () => {
  assert.equal(normalizeTheme("dark"), "dark");
  assert.equal(normalizeTheme("light"), "light");
  for (const junk of ["Dark", "LIGHT", "", " dark", "system", null, undefined, 0, 1,
                      [], {}, ["dark"], { theme: "dark" }, "../../../etc/passwd"]) {
    assert.equal(normalizeTheme(junk), null, `admitted ${JSON.stringify(junk)}`);
  }
});

test("both colour pairs keep the window controls legible (WCAG 1.4.11, 3:1)", () => {
  // symbolColor draws minimise/maximise/close on the Windows overlay. Too close
  // to the background and the controls are there but invisible — the user sees
  // a dead strip and cannot close the window with the mouse.
  const lin = (c) => (c /= 255, c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  const lum = (hex) => {
    const h = hex.replace("#", "");
    const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
  };
  for (const theme of ["dark", "light"]) {
    const { bg, symbol } = themeColors(theme);
    assert.match(bg, /^#[0-9A-Fa-f]{6}$/, `${theme} bg is not a hex colour`);
    assert.match(symbol, /^#[0-9A-Fa-f]{6}$/, `${theme} symbol is not a hex colour`);
    const [x, y] = [lum(bg), lum(symbol)].sort((p, q) => q - p);
    const r = (x + 0.05) / (y + 0.05);
    assert.ok(r >= 3, `${theme}: ${symbol} on ${bg} is ${r.toFixed(2)}:1`);
  }
  // And they are actually DIFFERENT pairs — one shared object would make the
  // light theme paint a dark frame and every assertion above still pass.
  assert.notEqual(themeColors("dark").bg, themeColors("light").bg);
  // An unknown theme must fall to the DEFAULT's colours, not to light's.
  assert.deepEqual(themeColors("nonsense"), themeColors(DEFAULT_THEME));
});

test("readTheme returns the default when there is nothing to read", () => {
  const dir = tmp();
  assert.equal(readTheme(dir), DEFAULT_THEME, "an empty dir");
  assert.equal(readTheme(path.join(dir, "does", "not", "exist")), DEFAULT_THEME);
});

test("readTheme survives every shape a bad file can take", () => {
  // A launch must never fail on this file. It is written by the app but a user
  // can edit it, a disk can truncate it, and a JSON document is legally a
  // string, an array or null.
  const dir = tmp();
  for (const raw of ["", "{", "null", "[]", '"light"', "42",
                     '{"theme":"purple"}', '{"theme":null}', '{"nope":1}',
                     '{"theme":["light"]}']) {
    fs.writeFileSync(path.join(dir, THEME_FILE), raw, "utf8");
    assert.equal(readTheme(dir), DEFAULT_THEME, `on ${JSON.stringify(raw)}`);
  }
});

test("a stored choice round-trips — both ways", () => {
  const dir = tmp();
  for (const theme of ["light", "dark", "light"]) {
    assert.equal(writeTheme(dir, theme), true, `writing ${theme}`);
    assert.equal(readTheme(dir), theme, `reading back ${theme}`);
  }
  // Written into a dir that does not exist yet, as on a first run.
  const fresh = path.join(tmp(), "userData");
  assert.equal(writeTheme(fresh, "light"), true);
  assert.equal(readTheme(fresh), "light");
});

test("writeTheme refuses junk instead of persisting it", () => {
  const dir = tmp();
  writeTheme(dir, "light");
  for (const junk of ["purple", "", null, undefined, {}, ["dark"], "system"]) {
    assert.equal(writeTheme(dir, junk), false, `wrote ${JSON.stringify(junk)}`);
  }
  assert.equal(readTheme(dir), "light", "a refused write still changed the file");
});

test("a write that cannot land is reported, not thrown",
  // A read-only directory is a POSIX-permission scenario: `chmod 0o500` is a
  // no-op on Windows (it toggles only FILE_ATTRIBUTE_READONLY, which does not
  // stop file creation inside a directory), so writeTheme would SUCCEED there and
  // the assertion "returns false" would wrongly fail. Making a directory truly
  // non-writable on Windows needs an icacls deny ACE, which is heavy and flaky;
  // the reported-not-thrown property is validated in full on macOS/Linux.
  { skip: process.platform === "win32"
    ? "read-only-directory is a POSIX-permission scenario; chmod 0o500 does not "
      + "make a directory non-writable on Windows" : false },
  () => {
  // A read-only home is a real deployment; it must cost one wrong-coloured
  // frame, never a crash on the way to the board.
  const dir = tmp();
  fs.chmodSync(dir, 0o500);
  try {
    assert.equal(writeTheme(dir, "light"), false);
  } finally {
    fs.chmodSync(dir, 0o700);
  }
});
