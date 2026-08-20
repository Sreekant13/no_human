# Cost levers — what actually moves the number (measured, 2026-07-14)

All figures below are **token-free**: they come from the live DB and `/api/metrics`, not from
spending quota to re-derive what the record already holds.

## The burn, decomposed (lifetime, 100 attempts)

| bucket | tokens | share | price |
|---|---:|---:|---|
| coder in/out (`tokens_used`) | 1,694,616 | 1.0% | full |
| cache **creation** | 6,014,457 | 3.4% | full |
| cache **read** | 168,178,356 | **95.6%** | 1/10 |
| **total** | **175,887,429** | | **$73.58** |

**The coder re-reading its own growing context is the whole game.** `read_per_attempt` is
**2,102,229**. Everything else is noise by comparison.

## Task 4.2 — the proposer turn budget: NOT a lever. Do not retune.

The #41 finding claimed *"on a complex task, 2 of 3 MoA proposers hit the 10-turn cap and drafted
nothing — ⅔ of the 3× Opus fan-out wasted."* On the **full record** that does not replicate:

```
16 plan generations recorded
  drafted nothing usable (<200 chars):  1  → (74 chars, 11 turns, 6,122 tokens)
  hit/exceeded the 10-turn cap:         1
  turns: median 5, max 11        (the cap is 10 — orchestrator.py, plan_cfg.max_turns)
  chars: median 3,693
```

One dud in sixteen, costing **6,122 tokens — 0.003% of the lifetime burn.** Raising the proposer
cap or cutting 3→2 proposers would be tuning against a phantom, and an n=1 live A/B (the plan's
own suggestion, which it flagged as weak evidence) could not distinguish the effect from noise.

**Decision: no change.** The lever is real but worth ~0.003%; the evidence needed to justify
spending quota on it does not exist, and the record says it isn't there.

## Task 4.3 — the coder's re-read context IS the lever (95.6%), and here is what is IN it

Measured from the DB (77 attempts with turns recorded, and the `prompt_size` telemetry):

```
avg turns per attempt           33.3
avg cache-read per attempt      2.18M
  → re-read PER TURN            65.5k tokens
coder seed prompt (median)      25,430 chars ≈ 6.4k tokens   → only ~10% of the per-turn re-read
PreCompact events ever fired    0
```

**Three facts that redirect this task:**

1. **SDK auto-compaction has NEVER fired** (0 `compact` events across the whole DB). Sessions end
   below the CLI's auto-compact threshold, so there is nothing to "engage" — do not go looking for
   a compaction knob, and do not rebuild SDK-owned compaction (per the project's lean-stack
   constraint: the product never re-implements capabilities the SDK it runs on
   already owns).
   **CORRECTION (coder-in-attempt cache-burn ticket, landed — see Lever 2 below):** "nothing to
   engage" was the wrong read of the same fact. Auto-compaction never fired because its window
   defaults to the ~200k-token full model context, and the coder's own context plateaus around
   170k — under the threshold, not past a ceiling that doesn't exist. The fix is still not a
   rebuild: it sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, an env var the bundled CLI already reads,
   to 140k for the coder role only. Configuring the SDK's own compaction is the opposite of
   re-implementing it.
2. **The seed is not the whale.** It was already dieted in C1 (−43%) and is now ~6.4k tokens —
   **10%** of the 65.5k re-read each turn. Halving it again would cut ~5% of the burn. Worth
   little.
3. **The other ~90% is the per-call floor plus the growing conversation** — the CLI's
   system+tools+env baseline (~20–24k, structural, probed in C1) plus the accumulating tool
   output the coder re-reads every single turn (file reads, test output, diffs).

**So the lever is the conversation, not the prompt**: what the coder drags forward across 33
turns. The candidates, in order of expected yield — each must be measured before/after on a fixed
task, and each is rejected if the review pass-rate moves:
- prune/summarise stale tool output (a file read 20 turns ago is re-read 20 times);
- isolate exploration in a sub-agent so its transcript never enters the main context;
- cut turns (33 median): every turn removed saves a full 65.5k re-read.

## Lever 2 — the coder's compaction window (landed)

The coder-in-attempt cache-burn ticket (refile of 83b51a6e). `agent/backend.py::make_backend`
now resolves `bounds.coder_compact_window_tokens` (default
`DEFAULT_CODER_COMPACT_WINDOW_TOKENS = 140_000`, fail-closed on a bad value) and passes it to
`ClaudeBackend(compact_window_tokens=...)`, which sets
`ClaudeAgentOptions.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"]` — scoped to the coder role only,
never reviewer/planner/utility/supervisor/distill. `orchestrator.py` adds `cache_burn` telemetry
(a running-total event every 10th coder message) and a per-attempt compaction counter
(`_on_coder_compact`), both surfaced live in `nh logs` — the observable proof the config actually
fires, since real compaction had 0 historical occurrences before this change.

**Measured before/after**, `scripts/measure_cache_burn.py --db ~/.no_human/no_human.db --role
coder` (652 real coder attempts, replaying each attempt's real recorded `cache_creation_tokens`
growth through a counterfactual: as-recorded — no compaction, matching the 0-events history above
— versus windowed at 140k):

```
652 real coder attempts, window=140,000 tokens, post-compact floor=30% of window
(estimate — no historical compaction event exists to measure this from):
  median modelled burn: 8,171,012 -> 5,952,536 (30.9% reduction)
  P95 modelled burn:    56,951,074 -> 19,370,621
  simulated compactions across all attempts: 789
```

**Honest limits, stated rather than buried:** this is an offline replay, not a live benchmark
re-run — no benchmark infrastructure was available in this session to launch real before/after
coder attempts, and the vendor CLI's actual compaction behaviour is opaque and cannot be
re-simulated exactly. The post-compaction floor (what compaction resets the running context DOWN
to) is an estimate (30% of the window) because zero historical compaction events exist to measure
it from; the growth *inputs* driving the replay are real, per-turn `cache_creation_tokens` figures
already recorded by the product, not synthetic. Both the as-recorded and windowed series are built
from the identical growth input, so the windowed total can only ever be lower — a structural
guarantee pinned by a regression test in `tests/test_measure_cache_burn.py`, which also caught and
fixed a real bug (an earlier version of the script diffed the noisier `cache_read_tokens` column
directly and produced attempts where "after" came out higher than "before").

## What the cost surfaces now say (and what they still cannot see)

The Stats page prices coder **and** reviewer (the reviewer's burn used to be discarded after the
verdict — 59 Opus-4-8 review passes cost $0 on the page). CORRECTED (B2 #5 — the previous
sentence here was FALSE): the planner/MoA proposers/aggregator run on separate readonly
backends; their burn was persisted nowhere and now lands in the attempts `plan_*` columns.
The supervisor's burn (small, also separate) is still uncaptured — B2 #6.

Every surface — the per-PR tile, the lifetime tile, the task table, the drawer header — divides
the SAME number by a different denominator, so they cannot disagree. They did, twice: $29.98/PR
against a $55.54 lifetime (pricing a total burn at the fresh rate), then $3.93 against $55.54
(splitting `tokens_per_pr` by `creation_share` — a category error: `tokens_per_pr` contains no
cache-creation at all, and it divides by prs_OPENED while wearing a per-MERGED-PR label).


## Task 4.5 — deterministic token-free tools: NO CLEAN CANDIDATE (measured, not assumed)

The plan's instruction was explicit: *"Only build a NEW deterministic op if the corpus proves it
recurs AND it's buildable without creds. Don't force it; document 'no clean candidate' honestly if
so."* Derived from a private analysis of recurring operations, which is not published:

| recurring op | count | deterministic? | creds-free? | verdict |
|---|---:|---|---|---|
| **review** (code review, triage comments) | 14 | **NO — it is judgment** | yes | stays AI. A token-free tool that *guesses* which finding matters is worse than an honest LLM call. `nh review` already exists. |
| **analytics** queries against a warehouse | 6 | yes | **NO** — warehouse creds | blocked on the same gate as the connectors |
| **message queue** offsets/partitions | 3 | yes | **NO** — cluster creds | same gate |
| **CI pipeline** lookups | 3 | yes | **NO** — forge CLI creds | same gate |
| repo **env** setup | 2 | yes | yes | **already covered** by `nh onboard` / `nh test` |

The `nh` surface is already 38 verbs wide (`onboard`, `recall`, `test`, `history`, `doctor`,
`merge-stack`, …). Every deterministic operation the corpus repeats is either **already a verb** or
**gated on the operator's credentials** — the same gate that blocks the connectors. There is no
operation left that is *recurring* AND *deterministic* AND *creds-free* AND *not already built*.

**Decision: build nothing.** Inventing a verb to satisfy the task would add surface area, not save
tokens. This unblocks the moment the staging credentials land in `~/.no_human/.env` — at which point
those deterministic lookups become buildable, and each is a genuinely
permanently token-free win.
