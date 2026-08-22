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
([`_make_guard_hook`](../src/no_human/agent/claude_backend.py)) that
enforces forbidden paths, protected branches, the merge ban and a
destructive-shell circuit breaker. A failed review loops back to implement.
Branching, committing and pushing are done by no_human's own git code, not by
the model. The PR lands in `awaiting_approval` and waits.

## An adversarial reviewer that is not the author

[`src/no_human/review/reviewer.py`](../src/no_human/review/reviewer.py) opens a
fresh Agent SDK session with read-only tools, on a different model from the
implementer by default (an Opus-tier reviewer over the Sonnet-tier coder —
the current IDs are `llm.review_model` and `llm.primary_model` in
[`DEFAULT_CONFIG`](../src/no_human/config.py)), and tells it to refute
"done". It returns a checklist of findings with `file`, `line` and severity — a
boolean verdict, never a score. Three things make that verdict hard to game:
every cited location is checked against the actual tree, and a finding citing a
location that does not exist is demoted to advisory (`_verify_citations` in
`reviewer.py`); the pass/fail is recomputed deterministically from the checklist
rather than taken on the model's word (`_gate_verdict` in `reviewer.py`); and a
reviewer that crashes, times out, or emits no parseable verdict fails closed
(`_parse_review_output`, `AdversarialReviewer._fast_review` and
`AdversarialReviewer._agent_review` in `reviewer.py`).

## Verifiers — a recorded verdict per rule

[`src/no_human/review/verifiers.py`](../src/no_human/review/verifiers.py) loads
project-specific rules from `.no_human/verifiers.yaml` (repo-scoped) and a
second, global file under `~/.no_human` — each rule a plain-English
`statement` plus a glob `paths` list and a `severity`. Before the agentic
reviewer runs, `Orchestrator._run_review` selects the rules whose `paths`
match the changed files and puts each one, independently, to a fresh
bounded judge call (max one turn) with the diff and read-only file access.
Every verdict is recorded — pass or fail, with `evidence`, `file`/`line`
when it names one, and which files it actually checked — never only the
failures. A verifier that returns no parseable verdict (a timeout, a crash,
an unparseable response) fails closed, the same posture as the agentic
reviewer itself.

The merge into the review decision is monotonic, not advisory noise the
reviewer can talk itself past: **any** failing verifier ends the round
before the agentic reviewer ever runs, appearing on the checklist as
`rule:<verifier id>`. Only when every selected verifier is satisfied does the
round proceed to the reviewer, and its own findings still apply on top. Every
verifier verdict is persisted on the attempt row (`attempts.verifier_results`)
and keyed into `task.context.verifier_results` by the commit SHA it judged,
so a later attempt's verdicts never overwrite an earlier one's. The same
verdicts render twice for a human: as a `Verifiers` row in the PR body's
Evidence table (`core/pr_evidence.py`'s `verifiers_pin()` — `"N of N
satisfied"` or `"K of N failed — id1, id2"`, folded behind a `<details>` list
of every rule), and as a per-verifier list in the board's Review tab. No
`.no_human/verifiers.yaml` (repo or global), verifiers disabled in config, no
usable diff, or a changed-path set that matches none of the loaded rules all
skip the step entirely and proceed straight to the agentic reviewer — this is
an added gate, not a replacement for it, and an empty rule set changes
nothing about what already ran.

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

## A reproduction gate that proves the fix fixed the bug

[`src/no_human/testing/repro_gate.py`](../src/no_human/testing/repro_gate.py)
takes the tests the coder says demonstrate its change, copies them into a
worktree at the merge base, and requires them to **fail there** and **pass on
the new tree**. A bugfix whose test also passes on the unfixed code has proved
nothing. Default mode is `advisory`, which still enforces for a Python bugfix
(`repro_gate.mode` in [`DEFAULT_CONFIG`](../src/no_human/config.py)).

## When it cannot finish

The loop is bounded and it is allowed to give up. `bounds.max_attempts` is 3 per
loop, `bounds.max_turns_per_attempt` is 500, and `bounds.lifetime_attempts` is 9
across resumes (the `bounds` block of
[`DEFAULT_CONFIG`](../src/no_human/config.py)). An identical tool call repeated
in a loop, or the same agent-error signature seen again, trips stuck detection —
`StuckDetector.record_tool_call` and `StuckDetector.record` in
[`core/bounds.py`](../src/no_human/core/bounds.py) — which resets context
instead of stacking more corrections on a confused session.

When it runs out, it does not invent a plausible diff. It classifies the blocker
into one of eleven categories — `MISSING_ACCESS`, `AMBIGUITY`, `SCOPE_EXPLOSION`,
`IMPOSSIBLE`, `QUOTA`, `BUDGET_EXHAUSTED` and five more
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
- **No published catch-rate for the reviewer.** The reviewer tier moved to
  `claude-opus-5` on 2026-07-26 and was reverted to `claude-opus-4-8` on
  2026-08-11, after an A/B scored Opus 5 lower on the seeded-defect corpus at
  roughly 3x the round duration. The corpus and its control set have also grown
  across those runs, and the committed run records do not state which model
  they measured, so quoting any of them would attribute a number to a
  configuration it may not describe. No number is published anywhere here. The
  method is in [REVIEWER_RECALL_METHOD.md](REVIEWER_RECALL_METHOD.md);
  regenerate with `nh bench report --reviewer-recall`.
- **The benchmark is self-run and you cannot reproduce it.** There is a harness
  that replays real past tasks through the real pipeline and scores against what
  the human actually did; the committed run is
  [NORTH_STAR_BENCH.md](NORTH_STAR_BENCH.md). Its specs pin to the author's
  local repo paths, so `nh bench run` skips them on your machine. The harness is
  reusable, the corpus is not. Success rate also moves several points between
  runs on identical specs because the coder is non-deterministic, so treat any
  single figure as a point estimate rather than a score. The card now says so
  in its own numbers: `nh bench run --trials N` replays each spec N times, and
  the three surfaces that print the headline in this repo — the `bench run`
  console line, the `bench publish` console line and the published report —
  take it from one function (`success_headline` in
  [`eval/northstar_card.py`](../src/no_human/eval/northstar_card.py)), so none
  of them can print the percentage without its Wilson 95% interval and its `n`.
  The web Stats panel is the one surface that does NOT call it — it renders the
  interval the card recorded, over the API — so it agrees by carrying the same
  fields rather than by construction.
  `pass^N` — the share of specs that passed EVERY trial, which is what
  separates a capability from a coin flip — rides with it above one trial.
  A results file that records neither is refused by `nh bench publish` unless a
  human overrides it, and the override is printed at the top of the report.
  Two honest limits on that interval, because it is easy to over-read:
  it is computed on the **effective** n, not the row count — trials of one spec
  are correlated, so `specs × trials` rows are worth somewhere between `specs`
  and `specs × trials` independent observations and the card discounts them by
  the measured intracluster correlation (a nominal 95% interval over pooled
  rows covered the true rate about half the time). And it bounds SAMPLING error
  only: it says nothing about whether this corpus resembles your work, which is
  the limit the first three sentences of this bullet are about.
- **No dollar figure is a billed number.** Every task carries an enforced spend
  cap, and the cap is denominated in **cost-weighted** tokens, not raw ones: a
  cache read counts 0.1 of a fresh input token and a cache write 1.25
  (`CACHE_READ_WEIGHT` and `CACHE_CREATION_WEIGHT` in
  [`core/pricing.py`](../src/no_human/core/pricing.py); enforced by
  `Orchestrator._check_lifetime_budget` in
  [`core/orchestrator.py`](../src/no_human/core/orchestrator.py), and the
  per-task ledger sums the raw classes for reporting — `compute_metrics` in
  [`core/metrics.py`](../src/no_human/core/metrics.py)). Summing the classes 1:1 measures conversation
  *length* rather than cost — one task was killed at "12.4M/12M tokens" having
  spent about a fourteenth of that in fresh-equivalent terms, which is why the
  cap was re-denominated on 2026-07-31. Cache reads still dominate the traffic:
  in this project's own lifetime measurement over 100 attempts they were
  **95.6%** of all tokens burned ([COST_LEVERS.md](COST_LEVERS.md)) — and even
  at a tenth of the weight they are the largest single line in the bill.
  Tooling that reports "tokens used" without them is reporting roughly 1% of
  the traffic. Real-work
  attempts in that record measure 12k–32k output tokens each, and a one-surface PR takes one
  to three attempts. Any dollar figure derived from that is an estimate, not an
  invoice. `nh logs <id>` shows spend against the cap per task.
- **The reviewer and implementer being different models is a default, not an
  enforced invariant.** You can configure them to the same model. Nothing stops
  you.
- **There is no deploy step.** The pipeline ends at an open PR. Shipping is a
  separate problem and not one this solves.
- **Language coverage is uneven.** `nh onboard` auto-derives a test command for
  pytest, `npm test` and `mvn`
  ([`DeclarationDeriver`](../src/no_human/onboard.py)); anything else you
  configure by hand. The tamper guard reads Python, JS/TS and Java test files.
  The reproduction gate defaults to pytest and routes other ecosystems through
  the project profile's `test_cmd`.

## The merge ban, in code

`gh pr merge`, `glab mr merge` and the equivalent REST calls are denied before
they execute (`_FORGE_MERGE` in
[`agent/guard.py`](../src/no_human/agent/guard.py)), and pushes to `main`,
`master` and `release/*` are denied too, by `_push_targets_protected` and
`evaluate` in `agent/guard.py`; the default patterns are `git.never_push_to` in
[`DEFAULT_CONFIG`](../src/no_human/config.py). The full safety model, including
the one-billing-path-per-run rule, is in [security.md](security.md).
