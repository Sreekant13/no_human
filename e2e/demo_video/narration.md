# The narrated sprint demo — script and timing map

One script, one audio track, two videos. The board (GUI) and the `nh` shell
(CLI) are recorded to the same 78.000 s clock, and this narration plays over
both — side by side or one at a time. `narration.py` is the machine-readable
source of truth for every number on this page. Tests assert the agreement in
BOTH directions — every line the map schedules is written here, and every line
written here is one the map schedules — so neither a line dropped from this
page nor a line that exists only on it can survive. (One direction used to be
checked, and a line added only here was invisible.)

**Voice direction.** Read flat and unhurried, like explaining a screen to a
colleague standing behind you — not like a trailer. Every sentence names
something that is literally on screen while it is spoken. Pause at the full
stops; the gaps are in the timing, don't rush to fill them. "no_human" is
spoken *no human*.

**Duration** 78.000 s · 30 fps · 2340 frames · fade in ends 0.60 · fade out
76.60–77.40.

---

## The script (read top to bottom; times are when each line starts)

### 1 · Hook — 0.60 s
> **0.80** — Monday. Sprint planning just handed you ten tickets.
>
> **4.40** — A webhook bug. A flaky test. A refactor nobody wants to touch.

### 2 · Handoff — 9.00 s
> **9.40** — Pick five. Hand them to no_human.
>
> **12.40** — Tag them in your tracker, or paste them in. That's the whole
> handoff.

### 3 · Parallel — 17.00 s
> **17.60** — Five agents start. In parallel, on real branches.
>
> **21.80** — Each one writes a plan before it writes code. Files, approach,
> tests. It gets held to that plan.
>
> **29.20** — The webhook fix is already editing code. The flaky test is
> being reproduced, not guessed at.
>
> **35.40** — Nothing here waits for you.

### 4 · Gate — 38.00 s
> **38.60** — Then the gate. An independent reviewer, in a fresh context,
> with one brief: prove this task is not done.
>
> **46.20** — It reads the diff line by line, and cites its evidence.
>
> **50.60** — The refactor came back once. It went again. No rubber stamps.

### 5 · Payoff — 56.00 s
> **56.40** — The result: five pull requests, each with tests and review
> evidence attached.
>
> **62.20** — The agent never merges. That click stays yours.
>
> **66.20** — You read. You approve.

### 6 · Close — 70.00 s
> **70.60** — no_human. Your sprint, run in parallel. You keep the merge
> button.

---

## Timing map — narration ↔ seconds ↔ what's on screen

The sprint fixture (`sprint.py`) times every status change off these beats,
and both recorders derive every scripted step from them, so the columns below
are enforced, not aspirational.

| Beat | Starts | Narration lines | GUI (board) | CLI (shell) |
| --- | --- | --- | --- | --- |
| 1 Hook | 0.60 | 0.80 / 4.40 | The board, quiet: one leftover PR in Review, empty Working lane | The shell, quiet: same one leftover task in its Review lane |
| 2 Handoff | 9.00 | 9.40 / 12.40 | Five cards land in Queued/Working, Jira keys in the titles | The same five tasks fill the shell's lanes, same keys |
| 3 Parallel | 17.00 | 17.60 / 21.80 / 29.20 / 35.40 | Statuses tick on every card; the drawer opens on the webhook fix: its written plan, then the live agent lanes | The webhook task is selected; its event stream fills the detail pane - models, plan, edits, tests |
| 4 Gate | 38.00 | 38.60 / 46.20 / 50.60 | The drawer shows the diff, then the review: PASSED, with evidence cited file:line; the drawer closes on the refactor, bounced back to Working for a second round | /diff prints the same diff in the terminal; the refactor's card drops back to Working, then returns |
| 5 Payoff | 56.00 | 56.40 / 62.20 / 66.20 | All five cards sit in Review with PR links; the drawer opens on the webhook fix and Approve is clicked | /logs replays the evidence trail - tests, tamper guard, lint, PR; /approve is typed and answered |
| 6 Close | 70.00 | 70.60 | The drawer closes; the whole board, five approved-or-waiting PRs, holds to the fade | The whole shell - full lanes, quiet prompt - holds to the fade |

Key synchronized moments (identical wall-second in both clips, driven by
`sprint.py`):

| Second | Event on both surfaces |
| --- | --- |
| 9.60–14.40 | The five tickets are created, staggered |
| 16.40–17.40 | All five start running — finishing before "Five agents start" at 17.60 |
| 38.40 | The hero (PAY-1382) enters independent review |
| 44.00 | ORD-2187 FAILS review and starts attempt 2 — the gate is real |
| 55.40 | The fifth PR reaches Review: all five are awaiting approval |
| 66.40 | The human approves the hero — the one human act in the piece |

## The sprint backlog (all synthetic)

Ten tickets in the tracker; the engineer keeps five, hands five to no_human.
No real person, company, product or employer is referenced anywhere.

Handed over: **PAY-1382** webhook retry double-charges on 502 (bug, the
hero) · **ORD-2211** deflake `test_checkout_totals` on CI (flaky test) ·
**PAY-1401** idempotency keys for the refund API (feature) · **NOTIF-412**
digest emails fire twice on Mondays (investigation) · **ORD-2187** extract
tax math out of CheckoutService (refactor, bounces off review once).

Kept: PAY-1407 chargeback CSV export · ORD-2216 search returns deleted
SKUs · NOTIF-433 unsubscribe 404s on legacy tokens · PLAT-961 rate-limit
login attempts · PLAT-990 backup restore drill.

## Why the copy is shaped like this

- **Short declaratives, one claim per line.** It is read aloud over a busy
  screen; subordinate clauses die there.
- **No hype vocabulary.** Nothing is "revolutionary" and nothing gets
  "supercharged"; the strongest words in the piece are *proves*, *cites*,
  and *yours* — all three are product mechanics, not adjectives.
- **The gate gets 18 seconds — more than the hook and handoff combined.**
  The reviewer refusing to rubber-stamp (and the refactor actually bouncing)
  is the claim competitors can't read aloud. Only the parallel beat is
  longer, and only because it carries four lines.
- **The close is the hard rule.** "You keep the merge button" is
  constraint #2 of the product, word for word in spirit — the promise is a
  fact.
