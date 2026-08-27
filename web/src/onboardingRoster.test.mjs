import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { ROLE_LABEL } from "./eventRoles.js";

// Guards the WELCOME step's agent roster grid (.ob-agents in Onboarding.jsx): the
// six cards must name the six live roles (ROLE_LABEL is the source of truth), use
// truthful colour tokens, still advertise the investigation capability (NOT as read-only),
// and stay legible on narrow viewports.

const SRC = dirname(fileURLToPath(import.meta.url));
const jsx = readFileSync(join(SRC, "Onboarding.jsx"), "utf8");
const rawCss = readFileSync(join(SRC, "styles.css"), "utf8");
const css = rawCss.replace(/\/\*[\s\S]*?\*\//g, "");

// Pull just the welcome-step agent grid block out of Onboarding.jsx so assertions
// don't accidentally match unrelated JSX elsewhere in the file.
const agentsBlock = jsx.match(/<div className="ob-agents">([\s\S]*?)\n {14}<\/div>/)?.[1];
assert.ok(agentsBlock, "could not locate the .ob-agents block in Onboarding.jsx");

const cardBlocks = [...agentsBlock.matchAll(/<div className="ob-agent ([\w-]+)">([\s\S]*?)<\/p>\s*<\/div>/g)]
  .map((m) => ({ cls: m[1], body: m[2] }));

test("the six agent cards use exactly the ROLE_LABEL names, in the mandated order", () => {
  const roleTexts = [...agentsBlock.matchAll(/ob-agent-role">([^<]+)</g)].map((m) => m[1]);
  assert.deepEqual(
    roleTexts,
    ["Supervisor", "Planner", "Coder", "Reviewer", "Watcher", "Orchestrator"],
    "roster order/names drifted from the operator-mandated Supervisor/Planner/Coder-first order",
  );
  assert.equal(roleTexts.length, 6, "expected exactly six agent cards");

  const expected = {
    supervisor: ROLE_LABEL.supervisor,
    planner: ROLE_LABEL.planner,
    agent: ROLE_LABEL.agent,
    reviewer: ROLE_LABEL.reviewer,
    watcher: ROLE_LABEL.watcher,
    worker: ROLE_LABEL.worker,
  };
  const order = ["supervisor", "planner", "agent", "reviewer", "watcher", "worker"];
  order.forEach((key, i) => {
    assert.equal(roleTexts[i], expected[key], `card ${i} must read the live ROLE_LABEL for "${key}"`);
  });
});

test("no card claims a nonexistent agent (Shepherd / Investigator are gone)", () => {
  assert.doesNotMatch(jsx, /Shepherd/, "Watcher card must not still read 'Shepherd'");
  assert.doesNotMatch(jsx, /Investigator/, "Orchestrator card must not still read 'Investigator'");
});

test("the intro paragraph carries the operator's 2026-08-09 copy, and does NOT call investigations read-only", () => {
  const lede = jsx.match(/<p className="ob-lede">([\s\S]*?)<\/p>/)?.[1] ?? "";
  // The operator dictated this paragraph verbatim on 2026-08-09 (dropping the
  // cost clause and the investigations sentence from the welcome card). The
  // pin keeps a later edit from drifting the approved copy; changing it again
  // is an operator decision, not a wording cleanup.
  assert.match(
    lede,
    /fields an <em>entire team of specialized agents at once<\/em>\. Each/,
    "the .ob-lede must open with the operator-approved 2026-08-09 sentence",
  );
  assert.match(
    lede,
    /You just review and approve\./,
    "the .ob-lede must end on the operator-approved close, without the cost clause",
  );
  // 🔴 THE GUARD USED TO DEMAND THE FALSEHOOD. Its name, its comment and its failure
  // message all said "read-only investigations", and `read-only` sat in the alternation
  // above — so a reviewer reading this file was told the retracted claim was REQUIRED.
  // The `investigation` kind is not read-only at runtime: the coder is instructed to
  // propose a fix and run the tests, and the report-only path is gated on
  // `if not repo.has_changes()`. The alternation made it pass either way, so the copy fix
  // swept README, Onboarding.jsx and the positioning doc while this test still pointed at
  // the old claim. A guard that can be satisfied by the thing it should forbid is worse
  // than no guard: it launders the falsehood as verified.
  assert.doesNotMatch(
    lede,
    /read-only/i,
    "the .ob-lede must NOT call investigations read-only — they are not, and this claim was retracted 2026-07-29",
  );
  // The headline is now the BINDING slogan (PRODUCT.md), verbatim and
  // do-not-reword: two sentences, the second set as a secondary line.
  assert.match(jsx, /From ticket to reviewed pull request\./);
  assert.match(jsx, /Free and open-source, on your machine\./);
  assert.doesNotMatch(
    jsx,
    /10×|10x in parallel/i,
    "the first-run headline must not re-introduce the unmeasured 10× claim",
  );
});

test("the Coder card uses the Coder colour token; the Orchestrator card uses the orchestrator token", () => {
  const coder = cardBlocks.find((c) => c.body.includes(">Coder<"));
  const orchestrator = cardBlocks.find((c) => c.body.includes(">Orchestrator<"));
  assert.ok(coder, "Coder card not found");
  assert.ok(orchestrator, "Orchestrator card not found");
  assert.equal(coder.cls, "ob-agent-agent", "Coder must use the Coder (green) token, not the Orchestrator's blue");
  assert.equal(orchestrator.cls, "ob-agent-worker", "Orchestrator must use the worker/orchestrator (blue) token");
});

test("styles.css defines the Coder token rule for both themes", () => {
  assert.match(css, /\.ob-agent-agent\s*\{\s*--a:\s*var\(--agent-agent\);?\s*\}/);
  const agentAgentDefs = [...rawCss.matchAll(/--agent-agent:/g)];
  assert.equal(agentAgentDefs.length, 2, "--agent-agent must be defined in both the dark and light theme blocks");
});

test(".ob-agents has a narrow-viewport override so labels stop clipping below ~500px", () => {
  assert.match(
    css,
    /\.ob-agents\s*\{[^}]*grid-template-columns:\s*repeat\(3,\s*1fr\)/,
    "base .ob-agents grid must stay 3 columns at desktop width",
  );
  assert.match(
    css,
    /@media[^{]*\{\s*\.ob-agents\s*\{[^}]*grid-template-columns:\s*1fr/,
    "expected a @media override collapsing .ob-agents to a single column on narrow viewports",
  );
  assert.match(
    css,
    /@media[^{]*\{\s*\.ob-agents\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*1fr\)/,
    "expected a @media override collapsing .ob-agents to two columns on mid-width viewports",
  );
});
