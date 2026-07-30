import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// "Create a new repo" from the composer (operator-requested; plan Task 5).
// Like pathInput.test.mjs, there is no React renderer in this harness, so
// these pin WIRING only - the click-through behaviour (stubbed POST filling
// the repository path) is measured in a real browser by e2e/composer.mjs.

const here = fileURLToPath(new URL(".", import.meta.url));
const read = (f) => readFileSync(here + f, "utf8");

test("api.js scaffoldRepo POSTs parent+name to /api/repos/scaffold", () => {
  const src = read("api.js");
  assert.match(src, /export async function scaffoldRepo\(parent, name\)/);
  assert.match(src, /\/api\/repos\/scaffold/);
  // Backend errors carry WHICH check failed in `detail` - the composer must
  // be able to surface it verbatim, so the helper must throw it. It goes
  // through detailMessage() because FastAPI's `detail` is a LIST on a 422 and
  // `new Error(d.detail)` rendered that as "[object Object]" (apiError.js).
  assert.match(src, /scaffoldRepo[\s\S]{0,400}detailMessage\(d,/);
});

test("the composer imports and calls scaffoldRepo", () => {
  const src = read("TaskComposer.jsx");
  assert.match(src, /import \{[^}]*\bscaffoldRepo\b[^}]*\} from "\.\/api\.js"/);
  assert.match(src, /await scaffoldRepo\(/);
});

test("the create-repo parent input is the shared PathInput with its own datalist id", () => {
  const src = read("TaskComposer.jsx");
  // A duplicate id would break the browser's input-datalist pairing for BOTH
  // the repo-path field and this one (pathInput.test.mjs pins uniqueness
  // across every usage; this pins the specific id the e2e drives).
  assert.match(src, /<PathInput[\s\S]{0,400}listId="composer-newrepo-parent"/);
});

test("scaffold failure is surfaced verbatim near the control, not swallowed", () => {
  const src = read("TaskComposer.jsx");
  // The error state set from the thrown detail must reach a rendered alert.
  assert.match(src, /setNewRepoError\((?:err|e)\.message\)/);
  assert.match(src, /\{newRepoError\}/);
});
