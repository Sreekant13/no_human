<div align="center">

# no_human

**Give it a ticket. Get back a pull request, with the evidence that it works.**

[![python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[getnohuman.com](https://getnohuman.com)

</div>

no_human takes a software ticket and works it to an open pull request without
supervision. It reads the codebase, plans, writes the change, gets the change
reviewed by a second model that never saw it being written, runs your tests, and
opens the PR. Then it stops.

It never merges. That is not a setting. `gh pr merge`, `glab mr merge` and the
equivalent REST calls are denied before they execute
([`src/no_human/agent/guard.py:130`](src/no_human/agent/guard.py)), and pushes to
`main`, `master` and `release/*` are denied too - the denial is at
[`guard.py:300`](src/no_human/agent/guard.py), the default patterns at
[`config.py:676`](src/no_human/config.py).

It runs on your machine, against your checkout, on your Claude credential.
Nothing is uploaded to a hosted sandbox. Only prompts reach the model API.

**[Watch it work a ticket](https://getnohuman.com/#demo)** - filing the task, the
review evidence that comes back with the diff, and the approve step, which is
yours.

## Quickstart

```bash
git clone <your-clone-url>/no_human.git && cd no_human
uv sync                                             # installs the `nh` entry point into .venv
uv run nh init                                      # token, config, first repo (about 2 minutes)
uv run nh task add --title "Fix the off-by-one in pagination" --repo ~/my-repo
```

Requires Python 3.12+, [uv](https://github.com/astral-sh/uv), git, and a Claude
credential. By default that is an OAuth token from `claude setup-token`
(personal subscription or enterprise, both first-class). If you would rather pay
Anthropic directly, set `llm.auth_mode: "api_key"` and put your own
`ANTHROPIC_API_KEY` in `~/.no_human/.env`.

`nh start` runs the web board, the task worker and the wake watcher together on
`127.0.0.1:8420`. Full walkthrough: [docs/quickstart.md](docs/quickstart.md).

## What stops it from shipping something broken

Three gates, and one input they run on. All of it is code, not prompt
instructions, and the two deterministic gates run before a reviewer token is
spent.

**An adversarial reviewer that is not the author.**
[`src/no_human/review/reviewer.py`](src/no_human/review/reviewer.py) opens a
fresh Agent SDK session with read-only tools, on a different model from the
implementer by default (`claude-opus-5` reviewing `claude-sonnet-5`,
[`config.py:624-626`](src/no_human/config.py)), and tells it to refute "done".
It returns a checklist of findings with `file`, `line` and severity - a boolean
verdict, never a score. Three things make that verdict hard to game: every cited
location is checked against the actual tree, and a finding citing a location
that does not exist is demoted to advisory (`reviewer.py:904`); the pass/fail is
recomputed deterministically from the checklist rather than taken on the model's
word (`_gate_verdict`, `reviewer.py:917`); and a reviewer that crashes, times
out, or emits no parseable verdict fails closed (`reviewer.py:880`, `:1136`).

**Deterministic lint evidence. Not a gate, an input.**
[`src/no_human/review/lint_evidence.py`](src/no_human/review/lint_evidence.py)
runs ruff over the changed Python files and attaches the findings to the review
context, so the reviewer judges against machine output instead of reading the
diff cold. It uses the target repo's own ruff config and attaches nothing if the
repo has none, so no_human never imposes its style on yours. It cannot block on
its own: any failure returns empty rather than stalling the review.

**A tamper guard against a self-gutted test suite.**
[`src/no_human/testing/tamper_guard.py`](src/no_human/testing/tamper_guard.py)
diffs test files separately from product code and fails on a net drop in test or
assertion count, a net increase in skip/xfail markers, a real assertion replaced
by a tautology, or a behaviour-faking `autouse` fixture appearing in a
`conftest.py`. No model judgement is involved. It covers Python, JS/TS, Java and
the `e2e/` tree.

**A reproduction gate that proves the fix fixes something.**
[`src/no_human/testing/repro_gate.py`](src/no_human/testing/repro_gate.py) takes
the tests the coder says demonstrate its change, copies them into a worktree at
the merge base, and requires them to **fail there** and **pass on the new tree**.
A bugfix whose test also passes on the unfixed code has proved nothing. Default
mode is `advisory`, which still enforces for a Python bugfix
([`config.py:739-758`](src/no_human/config.py)).

## When it cannot finish

The loop is bounded and it is allowed to give up. `bounds.max_attempts` is 3 per
loop, `bounds.max_turns_per_attempt` is 500, and `bounds.lifetime_attempts` is 9
across resumes ([`config.py:713-726`](src/no_human/config.py)). A repeated
identical tool call or a repeated error signature trips stuck detection, which
resets context instead of stacking more corrections on a confused session
(`orchestrator.py:737-795`).

When it runs out, it does not invent a plausible diff. It classifies the
blocker into one of ten categories - `MISSING_ACCESS`, `AMBIGUITY`,
`SCOPE_EXPLOSION`, `IMPOSSIBLE`, `QUOTA`, `BUDGET_EXHAUSTED` and four more
([`src/no_human/blockers/taxonomy.py`](src/no_human/blockers/taxonomy.py)) - and
either parks with a wake condition or escalates with a structured report and one
specific question. `nh blocked` lists what is parked; `nh reply <id> "answer"`
resumes it.

An honest escalation costs a minute to triage. A confident wrong diff costs an
hour to review.

## How it works

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
([`claude_backend.py:143`](src/no_human/agent/claude_backend.py)) that enforces
forbidden paths, protected branches, the merge ban and a destructive-shell
circuit breaker. A failed review loops back to implement. Branching, committing
and pushing are done by no_human's own git code, not by the model. The PR lands
in `awaiting_approval` and waits.

## Daily commands

```bash
nh start                             # board + worker + wake watcher
nh task add <issue-url> --repo ~/repo
nh status                            # needs-you / working / waiting / done
nh review <id>                       # the reviewer's evidence checklist
nh diff <id>                         # the diff it wants to ship
nh logs <id>                         # turns, spend against the cap, failures
nh approve <id>                      # records approval - you merge the PR
nh reject <id> --reason "..."        # send it back with feedback
```

`nh --help` lists the rest (onboarding, rules, skills, shadow runs, the eval
harness, the benchmark). Note that `nh watch <id>` *runs* a task in a
foreground TUI - it is not a read-only viewer, so do not point it at a task
`nh start` is already working.

## Configuration

`~/.no_human/config.yaml`, generated by `nh init`. Secrets go in
`~/.no_human/.env` (chmod 600, never in the repo, never in config).

| Setting | Default | What it does |
|---|---|---|
| `llm.auth_mode` | `subscription` | `subscription` (OAuth) or `api_key` (your own key) |
| `llm.primary_model` | `claude-sonnet-5` | The implementer |
| `llm.review_model` | `claude-opus-5` | The reviewer |
| `bounds.max_attempts` | `3` | Implement/review cycles in one loop |
| `bounds.max_turns_per_attempt` | `500` | Agent turns before an attempt is cut off |
| `server.port` | `8420` | Web board bind port |
| `concurrency.enabled` | `false` | Parallel task workers, each in its own worktree |
| `ci.enabled` | `false` | Trigger and poll GitLab CI, GitHub Actions, Jenkins or CircleCI |

Concurrency ships off, and `max_workers` defaults to 2 when you turn it on
([`config.py:830-835`](src/no_human/config.py)). Full reference:
[docs/configuration.md](docs/configuration.md).

In the default `subscription` mode, a present `ANTHROPIC_API_KEY` aborts startup
rather than being silently ignored (`config.assert_subscription_mode`,
[`config.py:546`](src/no_human/config.py)). Silently scrubbing it would hide a
misconfiguration that costs real money. In `api_key` mode the reverse holds: your
key is the billing path and every *other* metered route is scrubbed, so a run
bills exactly one thing and records which.

## Limits

Things this does not do, and numbers this does not have.

- **Ambitious tasks are not the target.** It is aimed at well-scoped work:
  bugfixes, test gaps, small features, investigations. A vague ticket produces
  an escalation, which is the intended behaviour, not a workaround.
- **No published catch-rate for the reviewer.** The last full measurement
  against the seeded-defect corpus ran on `claude-opus-4-8`. The shipping
  reviewer has been `claude-opus-5` since 2026-07-26 and has **not** been
  re-measured, and the one A/B that did run scored Opus 5 lower on that corpus.
  Quoting the old number would be attributing it to a model it does not
  describe, so no number is published anywhere here. The method is in
  [docs/REVIEWER_RECALL_METHOD.md](docs/REVIEWER_RECALL_METHOD.md); regenerate
  with `nh bench report --reviewer-recall`.
- **The benchmark is self-run and you cannot reproduce it.** There is a harness
  that replays real past tasks through the real pipeline and scores against what
  the human actually did; the committed run is
  [docs/NORTH_STAR_BENCH.md](docs/NORTH_STAR_BENCH.md). Its specs pin to the
  author's local repo paths, so `nh bench run` skips them on your machine. The
  harness is reusable, the corpus is not. Success rate also moves several points
  between runs on identical specs because the coder is non-deterministic, so
  treat any single figure as a point estimate rather than a score.
- **No dollar figure is a billed number.** See below.
- **The reviewer and implementer being different models is a default, not an
  enforced invariant.** You can configure them to the same model. Nothing stops
  you.
- **There is no deploy step.** The pipeline ends at an open PR. Shipping is a
  separate problem and not one this solves.
- **Language coverage is uneven.** `nh onboard` auto-derives a test command for
  pytest, `npm test` and `mvn` ([`onboard.py:220-261`](src/no_human/onboard.py));
  anything else you configure by hand. The tamper guard reads Python, JS/TS and
  Java test files. The reproduction gate is pytest-only.

## Cost

Every task carries an enforced spend cap, and the cap counts cache reads, not
just input and output tokens (`orchestrator.py:713-717`; the per-task ledger
sums the same term for reporting at `metrics.py:38`). That matters more than it
sounds. In this project's own
lifetime measurement over 100 attempts, cache reads were **95.6%** of all tokens
burned ([docs/COST_LEVERS.md](docs/COST_LEVERS.md)); priced at a tenth of the
output rate, they still dominated the bill. Tooling that reports "tokens used"
without them is reporting roughly 1% of the traffic.

Real-work attempts in that record measure 12k-32k output tokens each
([docs/NORTH_STAR_PAYOFF.md](docs/NORTH_STAR_PAYOFF.md)), and a one-surface PR
takes one to three attempts. Any dollar figure derived from that is an estimate,
not an invoice. `nh logs <id>` shows spend against the cap per task.

## Development

```bash
uv sync
uv run pytest -q
uv run nh --help
```

Design source of truth: [PLAN.md](PLAN.md). Implementation brief:
[BUILD.md](BUILD.md). Safety model: [docs/security.md](docs/security.md).
Evaluation harness: [docs/eval.md](docs/eval.md). Intake adapter setup:
[docs/adapters.md](docs/adapters.md).

Issues and pull requests are welcome. Run `uv run pytest -q` before submitting.

## License

MIT. See [LICENSE](LICENSE).
