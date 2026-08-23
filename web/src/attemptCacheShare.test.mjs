import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

// The per-attempt cache-read share is the earliest signal an attempt is
// heading for budget exhaustion. core.metrics.cache_read_share is the ONLY
// definition of this arithmetic — api/models.py computes the wire value from
// it, and the drawer only ever displays a value the API already computed.
// This test pins that chain end to end, reading every link from ITS OWN
// SOURCE rather than restating it: a test that hand-copies the field name
// passes happily while a rename or a re-derivation breaks the drawer.

const SRC = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(join(SRC, p), "utf8");

const slideOver = read("SlideOver.jsx");
const css = read("styles.css");
const models = read("../../src/no_human/api/models.py");
const metrics = read("../../src/no_human/core/metrics.py");

test("the backend function that owns this arithmetic actually exists", () => {
  assert.ok(
    /def cache_read_share\(/.test(metrics),
    "core/metrics.py must define cache_read_share — the one owner of this arithmetic",
  );
});

test("the wire shape declares the field AND populates it from the shared function", () => {
  assert.ok(
    models.includes("from ..core.metrics import cache_read_share"),
    "api/models.py must import the shared helper rather than re-deriving the ratio",
  );
  assert.ok(
    /cache_read_share:\s*float \| None = None/.test(models),
    "AttemptOut must declare cache_read_share",
  );
  assert.ok(
    /cache_read_share=cache_read_share\(/.test(models),
    "from_row must populate cache_read_share by calling the shared helper, not local arithmetic",
  );
});

test("the attempt row renders the share, with the read/rebuilt split visible", () => {
  assert.ok(
    slideOver.includes("a.cache_read_share != null &&"),
    "the attempt row must render the share, guarded against unmeasured attempts",
  );
  assert.ok(
    slideOver.includes('data-testid="attempt-cache"'),
    "it needs a stable test hook",
  );
  // The split must be visible somewhere on the element (title/tooltip counts),
  // not just the collapsed percentage — that is what "with the split visible" means.
  const cacheBlockMatch = slideOver.match(
    /a\.cache_read_share != null[\s\S]{0,400}?<\/div>\s*\)\s*\}/,
  );
  assert.ok(cacheBlockMatch, "could not locate the attempt-cache render block");
  const block = cacheBlockMatch[0];
  assert.match(block, /cache_read_tokens/, "the split must reference cache_read_tokens");
  assert.match(block, /cache_creation_tokens/, "the split must reference cache_creation_tokens");
});

test("the drawer never re-derives the ratio locally — it only ever displays the API's value", () => {
  // cache_read_share * 100 (formatting the already-computed share) is fine;
  // a fresh division of the raw token counts is not.
  const localDivision = /cache_read_tokens\s*\/\s*\(?\s*cache_read_tokens.*cache_creation_tokens/i;
  assert.ok(!localDivision.test(slideOver), "SlideOver.jsx must not re-derive the share locally");
});

test("styles.css defines the class the row renders", () => {
  assert.ok(/\.attempt-cache\s*\{/.test(css), "styles.css must define .attempt-cache");
});
