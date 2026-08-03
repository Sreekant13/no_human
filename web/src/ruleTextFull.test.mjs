import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// BUG (first external DMG tester): a rule card in the onboarding rules-review
// step showed text cut off mid-sentence, with no way to read the rest.
//
// The truncation was SERVER-side — `f.content[:400]` in
// src/no_human/history/ingester.py, guarded by
// tests/test_ingester.py::test_proposal_carries_the_whole_rule_text_not_a_slice.
// These are the client-side halves of the same fix:
//
//  1. the step must render the content it is given, whole — no slice, no clamp;
//  2. now that the text can be long, .ob-rule-text must stay scrollable-not-clipped,
//     the same way .memory-card-content already treats the identical content in
//     Settings. A line-clamp here would silently reinstate the bug in CSS, where
//     the Python test cannot see it.

const here = fileURLToPath(new URL(".", import.meta.url));
const jsx = readFileSync(here + "Onboarding.jsx", "utf8");
const css = readFileSync(here + "styles.css", "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

const RULES_STEP = (() => {
  const start = jsx.indexOf('{step.key === "rules" &&');
  assert.ok(start > 0, "the rules-review step must exist");
  const end = jsx.indexOf('{step.key === "summary" &&', start);
  assert.ok(end > start, "could not bound the rules step");
  return jsx.slice(start, end);
})();

const ruleTextRule = (() => {
  const m = css.match(/\n\.ob-rule-text\s*\{([^}]*)\}/);
  assert.ok(m, ".ob-rule-text must still be styled in styles.css");
  return m[1];
})();

test("the rule card renders the content whole — no client-side slice", () => {
  assert.match(RULES_STEP, /className="ob-rule-text">\{p\.content\}</,
    "the card must print p.content as given");
  assert.doesNotMatch(RULES_STEP, /p\.content\.slice\(|p\.content\.substr/,
    "the client must not re-introduce the truncation it was just freed from");
});

test(".ob-rule-text is never clipped — the full text stays reachable", () => {
  assert.doesNotMatch(ruleTextRule, /line-clamp/,
    "a line-clamp would put the mid-sentence cut back, in CSS");
  assert.doesNotMatch(ruleTextRule, /text-overflow/,
    "an ellipsis here hides text with no way to reach it");
  assert.doesNotMatch(ruleTextRule, /overflow\s*:\s*hidden/,
    "overflow:hidden clips the tail with no scrollbar to recover it");
  assert.doesNotMatch(ruleTextRule, /white-space\s*:\s*nowrap/,
    "nowrap turns a multi-line rule into one clipped line");
});

test(".ob-rule-text caps its height by SCROLLING, the .memory-card-content idiom", () => {
  // A verbatim user message can be very long; without a cap one proposal buries
  // the rest of the list. Capped + scrollable is what Settings already does with
  // the same content, so this is not a new interaction pattern.
  assert.match(ruleTextRule, /max-height\s*:\s*\d/,
    "a long rule must not blow out the list");
  assert.match(ruleTextRule, /overflow-y\s*:\s*auto/,
    "the capped height must scroll, so nothing is unreachable");
  assert.match(ruleTextRule, /white-space\s*:\s*pre-wrap/,
    "mined rules carry their own line breaks");
});

test("the container that holds the cards still scrolls too", () => {
  const rules = css.match(/\n\.ob-rules\s*\{([^}]*)\}/);
  assert.ok(rules, ".ob-rules must still be styled");
  assert.match(rules[1], /overflow\s*:\s*auto/,
    "capping .ob-rules height without a scroller would hide whole cards");
});
