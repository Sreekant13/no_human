import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import * as costModule from "./cost.js";

// Durable guard for the bug this file exists to keep fixed: `web/src/cost.js` used to price
// every attempt at one hardcoded Anthropic rate ($3/1K fresh, $0.3/1K cache-read), which was
// wrong for any Codex/OpenAI attempt. The fix moved pricing server-side (`core/pricing.py` /
// `core/cost.py`); the board now only formats `cost_usd` the API sends. A price table, a rate
// constant, or a `?? 0.003` fallback creeping back into any .js/.jsx file would silently
// reintroduce the exact bug this task fixed — this test scans the whole tree for that shape so
// a future edit cannot do it quietly.

const SRC = dirname(fileURLToPath(import.meta.url));

// Matches: RATE_ / _RATE identifiers, the two deleted literal rates (0.003, 0.0003) however
// spaced/divided, and PER_TOKEN-style naming — the shapes the deleted `RATE_FRESH_PER_TOKEN` /
// `RATE_CACHE_READ_PER_TOKEN` / `costOf` used. Deliberately NOT matching "cost_usd", "cost_model"
// or "cost.js" style tokens the API-driven fields legitimately use.
const FORBIDDEN = /\bRATE_[A-Z_]*\b|_PER_TOKEN\b|\b0\.003\b|\b0\.0003\b/;

function* walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === "node_modules" || entry.name.startsWith(".")) continue;
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      yield* walk(full);
    } else if (/\.(js|jsx)$/.test(entry.name) && !entry.name.endsWith(".test.mjs")) {
      yield full;
    }
  }
}

test("no rate constant or per-token price lives in any web/src .js or .jsx file", () => {
  const offenders = [];
  for (const path of walk(SRC)) {
    const text = readFileSync(path, "utf8");
    const m = text.match(FORBIDDEN);
    if (m) offenders.push(`${path}: ${JSON.stringify(m[0])}`);
  }
  assert.deepEqual(
    offenders,
    [],
    `a rate/price literal is back in JS — pricing must stay server-side (core/pricing.py):\n${offenders.join("\n")}`,
  );
});

test("cost.js no longer exports a cost-computing function — only formatters/readers", () => {
  assert.equal(costModule.costOf, undefined, "costOf was deleted; a caller must read task.cost_usd");
  assert.equal(costModule.estimateCost, undefined);
  assert.equal(typeof costModule.taskCost, "function");
  assert.equal(typeof costModule.lifetimeCost, "function");
  assert.equal(typeof costModule.fmtCost, "function");
});
