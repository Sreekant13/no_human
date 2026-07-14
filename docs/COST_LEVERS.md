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
   a compaction knob, and do not rebuild SDK-owned compaction (CLAUDE.md #6).
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

## What the cost surfaces now say (and what they still cannot see)

The Stats page prices coder **and** reviewer (the reviewer's burn used to be discarded after the
verdict — 59 Opus-4-8 review passes cost $0 on the page). The planner and supervisor run inside
the coder's session, so their burn is already in `tokens_used`.

Every surface — the per-PR tile, the lifetime tile, the task table, the drawer header — divides
the SAME number by a different denominator, so they cannot disagree. They did, twice: $29.98/PR
against a $55.54 lifetime (pricing a total burn at the fresh rate), then $3.93 against $55.54
(splitting `tokens_per_pr` by `creation_share` — a category error: `tokens_per_pr` contains no
cache-creation at all, and it divides by prs_OPENED while wearing a per-MERGED-PR label).
