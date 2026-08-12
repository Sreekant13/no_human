import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// Operator finding (demo walk): a finished ticket gave no way to OPEN its PR
// from the board. The card's PR affordance must be a real, labelled anchor -
// not a bare "PR" pill. Like pathInput.test.mjs there is no React renderer in
// this harness, so these assertions pin WIRING only; the click/keyboard
// BEHAVIOUR (drawer stays closed, anchor is tabbable) is measured in a real
// browser by e2e/board.mjs.

const here = fileURLToPath(new URL(".", import.meta.url));
const read = (f) => readFileSync(here + f, "utf8");

test("Board card renders a labelled View PR anchor for http(s) pr_urls", () => {
  const src = read("Board.jsx");
  // A real anchor, new tab, no opener/referrer leak.
  assert.match(src, /<a\s[^>]*className="card-pr-badge"[\s\S]{0,200}target="_blank"/);
  assert.match(src, /className="card-pr-badge"[\s\S]{0,300}rel="noreferrer noopener"/);
  // The label says what it does - "View PR" with the external-link mark - so
  // the affordance is discoverable, not a cryptic pill. "Open" reads as the
  // PR's OWN state (open vs closed/merged), which is wrong on a done task
  // whose PR was closed by the squash-merge - the label must never say that.
  assert.match(src, /className="card-pr-badge"[\s\S]{0,400}View PR ↗/);
  assert.doesNotMatch(src, /Open PR/);
});

test("clicking the card's PR anchor must not also open the drawer", () => {
  const src = read("Board.jsx");
  assert.match(src, /card-pr-badge"[\s\S]{0,300}onClick=\{\(e\) => e\.stopPropagation\(\)\}/);
});

test("non-http pr_urls (demo local-pr://) degrade to a text-only badge, never a dead link", () => {
  const src = read("Board.jsx");
  assert.match(src, /httpPrUrl\(task\.pr_url\)/);
  assert.match(src, /<span className="card-pr-badge">PR<\/span>/);
  // The non-clickable fallback must never carry an href.
  assert.doesNotMatch(src, /<span[^>]*className="card-pr-badge"[^>]*href/);
});

test("the card's keydown guard leaves the PR anchor's own Enter activation alone", () => {
  const src = read("Board.jsx");
  // Card-level Enter/Space handling must bail when the event comes from a
  // focused descendant (the PR link), or Enter on the link opens the drawer.
  assert.match(src, /if \(e\.target !== e\.currentTarget\) return;/);
});

// 5D moved DONE tasks off the board into the Outcomes TaskTable - the
// operator's "no button to get to the PR when the ticket is done" lives THERE.
test("TaskTable rows carry the same View PR anchor for http(s) pr_urls", () => {
  const src = read("TaskTable.jsx");
  assert.match(src, /<a\s[^>]*className="card-pr-badge[^"]*"[\s\S]{0,200}target="_blank"/);
  assert.match(src, /className="card-pr-badge[^"]*"[\s\S]{0,300}rel="noreferrer noopener"/);
  assert.match(src, /className="card-pr-badge[^"]*"[\s\S]{0,400}View PR ↗/);
  assert.doesNotMatch(src, /Open PR/);
  // Clicking the link must not also open the row's drawer.
  assert.match(src, /card-pr-badge[^"]*"[\s\S]{0,400}onClick=\{\(e\) => e\.stopPropagation\(\)\}/);
  // Non-http (demo local-pr://) degrades to text-only, same rule as the card.
  assert.match(src, /httpPrUrl\(t\.pr_url\)/);
});

// M1 (review): the http(s) guard must be ONE shared rule, not three drifting
// copies — every PR surface imports it from prUrl.js (behaviour of the guard
// itself is pinned by prUrl.test.mjs).
test("all three PR surfaces share the single prUrl.js guard", () => {
  for (const f of ["Board.jsx", "TaskTable.jsx", "slideOverSummary.js"]) {
    assert.match(read(f), /import \{ httpPrUrl \} from "\.\/prUrl\.js"/, f);
    assert.doesNotMatch(read(f), /pr_url\.startsWith\(/, `${f} must not keep a private startsWith guard`);
  }
});
