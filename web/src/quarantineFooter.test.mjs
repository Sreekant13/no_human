import test from "node:test";
import assert from "node:assert/strict";
import { quarantineFooterLabel } from "./quarantineFooter.js";

test("0 renders no footer", () => {
  assert.equal(quarantineFooterLabel(0), "");
});

test("undefined renders no footer", () => {
  assert.equal(quarantineFooterLabel(undefined), "");
});

test("1 uses the singular", () => {
  assert.equal(quarantineFooterLabel(1), "1 row quarantined (hidden)");
});

test("N uses the plural", () => {
  assert.equal(quarantineFooterLabel(7), "7 rows quarantined (hidden)");
});

test("the label is built only from the count and fixed words, never row content", () => {
  // quarantineFooterLabel's signature takes only a count — there is no
  // parameter through which a row title or matched term could reach the
  // label. This pins the exact fixed vocabulary so a future edit that wires
  // in row content changes this assertion, not silently.
  for (const n of [1, 2, 3, 42]) {
    assert.match(quarantineFooterLabel(n), /^\d+ rows? quarantined \(hidden\)$/);
  }
});
