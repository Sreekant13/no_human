// Contrast is COMPUTED here, not pattern-matched: the point is the ratio, and a
// regex over the stylesheet would happily pass a value that fails WCAG.
// Interactive control boundaries need 3:1 (WCAG 1.4.11 Non-text Contrast), and
// the credential input is the only control on the first screen a user ever sees.
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";

const lin = (c) => (c /= 255, c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const lum = (hex) => {
  const h = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
};
const ratio = (a, b) => {
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
};

/** Every `--name: #hex` in the file, in order, so later (light) wins per theme. */
function tokens(page, { light }) {
  const css = fs.readFileSync(new URL(`./${page}`, import.meta.url), "utf8");
  const scope = light ? css.slice(css.indexOf("prefers-color-scheme: light")) : css;
  const out = {};
  for (const m of scope.matchAll(/(--[a-z-]+)\s*:\s*(#[0-9A-Fa-f]{6})/g)) {
    if (!(m[1] in out)) out[m[1]] = m[2];
  }
  return out;
}

test("control boundaries meet WCAG 1.4.11 (3:1) in both themes", () => {
  for (const page of ["token.html", "error.html"]) {
    for (const light of [false, true]) {
      const t = tokens(page, { light });
      const edge = t["--border-control"];
      assert.ok(edge, `${page} (${light ? "light" : "dark"}) defines no --border-control`);
      for (const surface of ["--bg", "--bg-raised", "--raised"]) {
        if (!t[surface]) continue;
        const r = ratio(edge, t[surface]);
        assert.ok(r >= 3,
          `${page} ${light ? "light" : "dark"}: ${edge} on ${surface} ${t[surface]} `
          + `is ${r.toFixed(2)}:1, below the 3:1 required for a control edge`);
      }
    }
  }
});

/** The custom property a rule's `border:`/`border-color:` actually references. */
function borderVarOf(page, selector) {
  const css = fs.readFileSync(new URL(`./${page}`, import.meta.url), "utf8");
  const at = css.indexOf(selector);
  assert.ok(at >= 0, `${page} has no ${selector} rule`);
  const block = css.slice(at, css.indexOf("}", at));
  const m = block.match(/border(?:-color)?\s*:[^;]*var\((--[a-z-]+)\)/);
  assert.ok(m, `${selector} in ${page} sets no border colour from a token`);
  return m[1];
}

test("the controls REFERENCE the accessible token, not just define it", () => {
  // Pointing the input back at --border (1.29:1 on --bg-raised) restored the
  // exact defect this was opened to fix while the token test stayed green.
  for (const [page, selector] of [["token.html", "input {"],
                                  ["token.html", "button {"],
                                  ["error.html", "a.retry.secondary {"]]) {
    const varName = borderVarOf(page, selector);
    for (const light of [false, true]) {
      const t = tokens(page, { light });
      const edge = t[varName];
      assert.ok(edge, `${page} ${selector} uses ${varName}, which is undefined`);
      for (const surface of ["--bg", "--bg-raised", "--raised"]) {
        if (!t[surface]) continue;
        const r = ratio(edge, t[surface]);
        assert.ok(r >= 3, `${page} ${selector} border ${varName}=${edge} on `
          + `${surface} is ${r.toFixed(2)}:1, below 3:1`);
      }
    }
  }
});

test("body text still meets AA (4.5:1) in both themes", () => {
  for (const light of [false, true]) {
    const t = tokens("token.html", { light });
    for (const fg of ["--text", "--text-hi", "--text-muted"]) {
      const r = ratio(t[fg], t["--bg"]);
      assert.ok(r >= 4.5, `${fg} on --bg is ${r.toFixed(2)}:1 (${light ? "light" : "dark"})`);
    }
  }
});
