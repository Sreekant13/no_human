<div align="center">

# no_human

**Give it a ticket. Get back a pull request, with the evidence that it works.**

[![python](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[getnohuman.com](https://getnohuman.com) · [Quickstart](docs/quickstart.md) · [Docs](docs/README.md)

</div>

[![The no_human board and the nh shell side by side at the end of a sprint: a queue of pull requests waiting on you, and the shell listing what it checked on the one in focus](docs/assets/demo-sprint-still.png)](https://getnohuman.com/#demo)

**[▶ Watch it work a ticket](https://getnohuman.com/#demo)** — filing the task,
the review evidence that comes back with the diff, and the approve step, which
is yours. Board and CLI, side by side.

The still above is one frame: a sprint's worth of tickets worked in parallel,
the pull requests queued for you, and the shell printing what it checked on the
one in focus — tamper guard, lint, tests, the commit, the PR. Both panes are the
shipped UI, driven frame by frame from a fixed fixture rather than a live run,
so the recording is reproducible and no model is called while it records; the
recorder is [`e2e/demo_video/`](e2e/demo_video). The figures on screen are that
fixture's, not a measurement.

no_human runs on your machine, against your checkout, on your Claude
credential. It reads the codebase, plans, writes the change, has it reviewed by
a second model that never saw it being written, runs your tests, opens the PR —
and stops. Only prompts leave your machine.

## Install

```bash
git clone <your-clone-url>/no_human.git && cd no_human
uv sync                 # installs the `nh` entry point into .venv
uv run nh init          # token, config, first repo (about 2 minutes)
```

Needs Python 3.12+, [uv](https://github.com/astral-sh/uv), git, and a Claude
credential — by default an OAuth token from `claude setup-token` (personal
subscription or enterprise, both first-class). To pay Anthropic directly
instead, set `llm.auth_mode: "api_key"` and put your own `ANTHROPIC_API_KEY` in
`~/.no_human/.env`.

## Run one task

```bash
nh start                             # board + worker + wake watcher on 127.0.0.1:8420
nh task add https://github.com/org/repo/issues/42 --repo ~/git/repo
nh status                            # needs-you / working / waiting / done
nh review <id>                       # the reviewer's evidence checklist
nh diff <id>                         # the diff it wants to ship
nh logs <id>                         # turns, spend against the cap, failures
nh approve <id>                      # records approval — you merge the PR
nh reject <id> --reason "..."        # send it back with feedback
```

A ticket is a GitHub or GitLab issue URL, or a plain-English `--title`. The task
ends in `awaiting_approval` with a PR open, and waits for you. `nh --help` lists
the rest — onboarding, rules, skills, shadow runs, the eval harness, the
benchmark. Full walkthrough: [docs/quickstart.md](docs/quickstart.md).

## What it does

- **Plans before it codes**, from the ticket plus what it finds by grepping your
  repo and reading its own past sessions.
- **Writes the change** through the Claude Agent SDK, on your credential, in
  your checkout — behind a `PreToolUse` hook that denies forbidden paths,
  protected branches and destructive shell commands.
- **Has it reviewed by a fresh-context adversary**: a different model, read-only
  tools, told to refute "done". The verdict is a pass/fail checklist with cited
  file and line — never a numeric self-score — and it is recomputed from the
  checklist rather than taken on the model's word.
- **Refuses a gutted test suite.** A net drop in tests or assertions, a new
  skip/xfail, an assertion replaced by a tautology: blocked deterministically,
  before a reviewer token is spent.
- **Proves the fix fixes something.** The tests the coder offers as evidence
  must fail at the merge base and pass on the new tree.
- **Runs your tests**, locally and optionally through your CI, and retries only
  infrastructure failures.
- **Opens the PR and stops**, or gives up honestly: it classifies the blocker
  into one of ten categories and parks with one specific question rather than
  inventing a plausible diff.

How each of those is enforced, with the code:
[docs/verification.md](docs/verification.md).

## Safety model

**It never merges.** `gh pr merge`, `glab mr merge` and the equivalent REST
calls are denied before they execute, pushes to `main`/`master`/`release/*` are
refused at the git layer, and there is no auto-merge setting to find. Merge is
your click. Git is driven by no_human's own code under a distinct commit
identity, not by the model; during review the backend is read-only. Credentials
live in `~/.no_human/.env` (`chmod 600`), never in the repo, and startup
guarantees a run bills exactly one path. Detail: [docs/security.md](docs/security.md).

## Cost

Every task carries an enforced spend cap, denominated in **cost-weighted**
tokens — a cache read counts a tenth of a fresh input token, not one for one,
because summing them equally measures how long the conversation got rather than
what it cost. Cache reads are still the bulk of the traffic: 95.6% of all tokens
in this project's own lifetime measurement over 100 attempts
([docs/COST_LEVERS.md](docs/COST_LEVERS.md)). No dollar figure here is a billed
number; `nh logs <id>` shows real spend against the cap, per task.

## What it does not do

- **A vague ticket escalates.** The limit is CLARITY, not the kind of work: a
  feature, a bug, a refactor and an investigation are all in scope, and the
  intake grill exists to turn a rough ask into a spec with testable acceptance
  criteria. What it will not do is guess. A ticket nobody can make concrete
  comes back as an escalation with a question, which is the intended behaviour
  and not a workaround.
- **No published catch-rate for the reviewer.** The last full measurement
  against the seeded-defect corpus ran on `claude-opus-4-8`. The shipping
  reviewer has been `claude-opus-5` since 2026-07-26 and has **not** been
  re-measured, and the one A/B that did run scored Opus 5 lower on that corpus.
  Quoting the old number would be attributing it to a model it does not
  describe, so no number is published anywhere here. The method is in
  [docs/REVIEWER_RECALL_METHOD.md](docs/REVIEWER_RECALL_METHOD.md); regenerate
  with `nh bench report --reviewer-recall`.
- **The benchmark is self-run and you cannot reproduce it.** Its specs pin to
  the author's local repo paths, so `nh bench run` skips them on your machine,
  and the coder is non-deterministic, so any single figure is a point estimate.
  The committed run: [docs/NORTH_STAR_BENCH.md](docs/NORTH_STAR_BENCH.md).
- **There is no deploy step**, and language coverage is uneven — the
  reproduction gate is pytest-only. The full list:
  [docs/verification.md](docs/verification.md#limits--things-this-does-not-do-and-numbers-it-does-not-have).

## Docs

| | |
|---|---|
| [quickstart.md](docs/quickstart.md) | Zero to first task |
| [configuration.md](docs/configuration.md) | `~/.no_human/config.yaml`, every setting and default |
| [verification.md](docs/verification.md) | The gates, the bounded loop, the limits |
| [security.md](docs/security.md) | Auth boundary, the never-merge rule, guards |
| [blockers.md](docs/blockers.md) | Taxonomy, escalation, wake watcher, `nh reply` |
| [adapters.md](docs/adapters.md) | Intake, context, VCS and CI backends |
| [eval.md](docs/eval.md) | Golden set, replay scoring, shadow mode |

Design source of truth: [PLAN.md](PLAN.md). Implementation brief:
[BUILD.md](BUILD.md).

## Development

```bash
uv sync
uv run pytest -q
uv run nh --help
```

Issues and pull requests welcome; run `uv run pytest -q` before submitting.

## License

MIT — see [LICENSE](LICENSE). The licence covers the code, not the name:
[TRADEMARK.md](TRADEMARK.md) is the policy on using "no_human" and the logo.
Packaging a binary carries obligations the source tree does not, listed in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
