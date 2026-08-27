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
  // docs has no pane of its own — it must resolve to a REAL Settings pane
  // (projects), not the generic fallback (the deep-link bug a real user hit).
  assert.deepEqual(deferredItems(["docs"]), [{ key: "docs", title: "Repo docs & wiki", page: "settings", tab: "projects" }]);
});

test("every deferred item resolves to a real Settings pane", () => {
  // The four steps the minimal path can defer. None may keep a tab that
  // Settings.jsx has no pane for, or the deep-link falls back to Projects.
  const PANES = new Set(["projects", "rules", "skills", "learnings", "integrations", "models", "account", "insights", "updates"]);
  for (const it of deferredItems(["docs", "integrations", "history", "rules"])) {
    assert.ok(PANES.has(it.tab), `${it.key} → ${it.tab} is not a Settings pane`);
  }
});

test("deferred items keep the server's order and drop unknown keys", () => {
  const keys = deferredItems(["rules", "docs", "bogus"]).map((i) => i.key);
  assert.deepEqual(keys, ["rules", "docs"]);
});

// ── wiring ──────────────────────────────────────────────────────────────────
const here = fileURLToPath(new URL(".", import.meta.url));

test("the repos step footer offers a self-explanatory skip-setup button, gated on canStartMinimal", () => {
  const src = readFileSync(here + "Onboarding.jsx", "utf8");
  // Copy must say it ENDS setup now — the old "Start with this repo" gave no
  // hint that it finished onboarding and confused a real user.
  assert.match(src, /Skip setup — open the board/);
  assert.match(src, /add integrations, docs &amp; rules later from Settings/);
  assert.match(src, /canStartMinimal\(/);
});

test("FinishSetupCard is a compact sidebar affordance over deferredItems", () => {
  const src = readFileSync(here + "FinishSetupCard.jsx", "utf8");
  assert.match(src, /deferredItems\(/);
  // The collapsed entry: a navrow-styled button labelled "Finish setup" with a
  // count badge — not the old board-body card.
  assert.match(src, /Finish setup/);
  assert.match(src, /nh-navrow-badge/);
});
