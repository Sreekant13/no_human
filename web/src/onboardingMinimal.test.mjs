import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { canStartMinimal, deferredItems } from "./onboardingMinimal.js";

// spec §3 B1: a real user wanted to start after picking ONE repo instead of
// walking eight linear steps. These pin the two predicates; the wiring (the
// button in the repos footer, the card on the board) is checked below by source
// assertion because this project's `node --test` harness has no React renderer.

test("minimal start needs at least one repo", () => {
  assert.equal(canStartMinimal({ selectedRepos: new Set() }), false);
  assert.equal(canStartMinimal({ selectedRepos: new Set(["/r"]) }), true);
});

test("deferred items name the settings pane", () => {
  assert.deepEqual(deferredItems(["docs"]), [{ key: "docs", title: "Repo docs & wiki", page: "settings", tab: "docs" }]);
});

test("deferred items keep the server's order and drop unknown keys", () => {
  const keys = deferredItems(["rules", "docs", "bogus"]).map((i) => i.key);
  assert.deepEqual(keys, ["rules", "docs"]);
});

// ── wiring ──────────────────────────────────────────────────────────────────
const here = fileURLToPath(new URL(".", import.meta.url));

test("the repos step footer really offers 'Start with this repo', gated on canStartMinimal", () => {
  const src = readFileSync(here + "Onboarding.jsx", "utf8");
  assert.match(src, /Start with this repo/);
  assert.match(src, /canStartMinimal\(/);
});

test("FinishSetupCard renders deferredItems and carries the exact copy", () => {
  const src = readFileSync(here + "FinishSetupCard.jsx", "utf8");
  assert.match(src, /deferredItems\(/);
  assert.match(src, /Finish setup — optional\. Each item opens in Settings\./);
});
