# What stops it shipping something broken — and what it does not cover

This is the detail the front page links to. It was the README's longest section
until 2026-08-01; nothing here was deleted, only moved off a page whose job is
to get you to a first task.

Three gates, and one input they run on. All of it is code, not prompt
instructions, and the two deterministic gates run before a reviewer token is
spent.

## The pipeline

```
ticket ──► context ──► plan ──► implement ──► review ──► test ──► PR ──► you merge
              │                      │           │         │
              │                      │           │         └── local runner + optional CI
              │                      │           └── fresh-context reviewer, read-only
              │                      └── Claude Agent SDK, your credential, your checkout
              └── grep, git log, past sessions
```

Tickets come from a GitHub or GitLab issue URL, or a plain-English `--title`.
Jira is supported as an opt-in server-side poller, not as an argument to
`nh task add`.

Implementation runs behind a `PreToolUse` hook
([`claude_backend.py:143`](../src/no_human/agent/claude_backend.py)) that
enforces forbidden paths, protected branches, the merge ban and a
destructive-shell circuit breaker. A failed review loops back to implement.
Branching, committing and pushing are done by no_human's own git code, not by
the model. The PR lands in `awaiting_approval` and waits.

## An adversarial reviewer that is not the author

[`src/no_human/review/reviewer.py`](../src/no_human/review/reviewer.py) opens a
fresh Agent SDK session with read-only tools, on a different model from the
implementer by default (`claude-opus-5` reviewing `claude-sonnet-5`,
[`config.py:624-626`](../src/no_human/config.py)), and tells it to refute
"done". It returns a checklist of findings with `file`, `line` and severity — a
boolean verdict, never a score. Three things make that verdict hard to game:
every cited location is checked against the actual tree, and a finding citing a
location that does not exist is demoted to advisory (`reviewer.py:904`); the
pass/fail is recomputed deterministically from the checklist rather than taken
on the model's word (`_gate_verdict`, `reviewer.py:917`); and a reviewer that
crashes, times out, or emits no parseable verdict fails closed
(`reviewer.py:880`, `:1136`).

## Deterministic lint evidence — not a gate, an input

[`src/no_human/review/lint_evidence.py`](../src/no_human/review/lint_evidence.py)
runs ruff over the changed Python files and attaches the findings to the review
context, so the reviewer judges against machine output instead of reading the
diff cold. It uses the target repo's own ruff config and attaches nothing if the
repo has none, so no_human never imposes its style on yours. It cannot block on
its own: any failure returns empty rather than stalling the review.

## A tamper guard against a self-gutted test suite

[`src/no_human/testing/tamper_guard.py`](../src/no_human/testing/tamper_guard.py)
diffs test files separately from product code and fails on a net drop in test or
assertion count, a net increase in skip/xfail markers, a real assertion replaced
by a tautology, or a behaviour-faking `autouse` fixture appearing in a
`conftest.py`. No model judgement is involved. It covers Python, JS/TS, Java and
the `e2e/` tree.

## A reproduction gate that proves the fix fixes something

[`src/no_human/testing/repro_gate.py`](../src/no_human/testing/repro_gate.py)
takes the tests the coder says demonstrate its change, copies them into a
worktree at the merge base, and requires them to **fail there** and **pass on
the new tree**. A bugfix whose test also passes on the unfixed code has proved
nothing. Default mode is `advisory`, which still enforces for a Python bugfix
([`config.py:739-758`](../src/no_human/config.py)).

## When it cannot finish

The loop is bounded and it is allowed to give up. `bounds.max_attempts` is 3 per
loop, `bounds.max_turns_per_attempt` is 500, and `bounds.lifetime_attempts` is 9
across resumes ([`config.py:713-726`](../src/no_human/config.py)). An identical
tool call repeated in a loop (`orchestrator.py:878`) or the same agent-error
signature seen again (`orchestrator.py:2470`) trips stuck detection, which
resets context instead of stacking more corrections on a confused session.

When it runs out, it does not invent a plausible diff. It classifies the blocker
into one of ten categories — `MISSING_ACCESS`, `AMBIGUITY`, `SCOPE_EXPLOSION`,
`IMPOSSIBLE`, `QUOTA`, `BUDGET_EXHAUSTED` and four more
([`src/no_human/blockers/taxonomy.py`](../src/no_human/blockers/taxonomy.py)) —
and either parks with a wake condition or escalates with a structured report and
one specific question. `nh blocked` lists what is parked; `nh reply <id>
"answer"` resumes it. Routing per category: [blockers.md](blockers.md).

An honest escalation costs a minute to triage. A confident wrong diff costs an
hour to review.

## Limits — things this does not do, and numbers it does not have

- **Ambitious tasks are not the target.** It is aimed at well-scoped work:
  bugfixes, test gaps, small features, investigations. A vague ticket produces
  an escalation, which is the intended behaviour, not a workaround.
- **No published catch-rate for the reviewer.** The last full measurement
  against the seeded-defect corpus ran on `claude-opus-4-8`. The shipping
  reviewer has been `claude-opus-5` since 2026-07-26 and has **not** been
  re-measured, and the one A/B that did run scored Opus 5 lower on that corpus.
  Quoting the old number would be attributing it to a model it does not
  describe, so no number is published anywhere here. The method is in
  [REVIEWER_RECALL_METHOD.md](REVIEWER_RECALL_METHOD.md); regenerate with
  `nh bench report --reviewer-recall`.
- **The benchmark is self-run and you cannot reproduce it.** There is a harness
  that replays real past tasks through the real pipeline and scores against what
  the human actually did; the committed run is
  [NORTH_STAR_BENCH.md](NORTH_STAR_BENCH.md). Its specs pin to the author's
  local repo paths, so `nh bench run` skips them on your machine. The harness is
  reusable, the corpus is not. Success rate also moves several points between
  runs on identical specs because the coder is non-deterministic, so treat any
  single figure as a point estimate rather than a score.
- **No dollar figure is a billed number.** Every task carries an enforced spend
  cap, and the cap is denominated in **cost-weighted** tokens, not raw ones: a
  cache read counts 0.1 of a fresh input token and a cache write 1.25
  ([`src/no_human/core/pricing.py:43`](../src/no_human/core/pricing.py);
  enforced at
  `orchestrator.py:845`, and the per-task ledger sums the raw classes for
  reporting at `metrics.py:38`). Summing the classes 1:1 measures conversation
  *length* rather than cost — one task was killed at "12.4M/12M tokens" having
  spent about a fourteenth of that in fresh-equivalent terms, which is why the
  cap was re-denominated on 2026-07-31. Cache reads still dominate the traffic:
  in this project's own lifetime measurement over 100 attempts they were
  **95.6%** of all tokens burned ([COST_LEVERS.md](COST_LEVERS.md)) — and even
  at a tenth of the weight they are the largest single line in the bill.
  Tooling that reports "tokens used" without them is reporting roughly 1% of
  the traffic. Real-work
  attempts in that record measure 12k–32k output tokens each
  ([NORTH_STAR_PAYOFF.md](NORTH_STAR_PAYOFF.md)), and a one-surface PR takes one
  to three attempts. Any dollar figure derived from that is an estimate, not an
  invoice. `nh logs <id>` shows spend against the cap per task.
- **The reviewer and implementer being different models is a default, not an
  enforced invariant.** You can configure them to the same model. Nothing stops
  you.
- **There is no deploy step.** The pipeline ends at an open PR. Shipping is a
  separate problem and not one this solves.
- **Language coverage is uneven.** `nh onboard` auto-derives a test command for
  pytest, `npm test` and `mvn`
  ([`onboard.py:220-261`](../src/no_human/onboard.py)); anything else you
  configure by hand. The tamper guard reads Python, JS/TS and Java test files.
  The reproduction gate is pytest-only.

## The merge ban, in code

`gh pr merge`, `glab mr merge` and the equivalent REST calls are denied before
they execute ([`src/no_human/agent/guard.py:130`](../src/no_human/agent/guard.py)),
and pushes to `main`, `master` and `release/*` are denied too — the denial is at
[`guard.py:300`](../src/no_human/agent/guard.py), the default patterns at
[`config.py:676`](../src/no_human/config.py). The full safety model, including
the one-billing-path-per-run rule, is in [security.md](security.md).
