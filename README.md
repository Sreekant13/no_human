<div align="center">

# no_human

**Give it a ticket. Get back a pull request you can actually review.**

![python](https://img.shields.io/badge/python-3.12%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

</div>

no_human runs the whole delivery loop on its own — intake, plan, implement,
adversarial review, tests, PR (and remote CI, once you enable it) — on **your
existing Claude subscription** rather than a metered API key. Then it stops and
waits for you.

```bash
nh task add --title "Fix the off-by-one in pagination" --repo ~/my-repo
```

**It never merges.** Work that reaches a PR stops at `awaiting_approval` with a
diff and an evidence checklist; work that can't be finished honestly stops at an
escalation instead. Merging is yours either way — that isn't a setting you can
flip, it's denied at the tool boundary and covered by tests.

Tasks come from a GitHub or GitLab issue URL, or a plain-English title. (Jira is
supported as an opt-in server-side poller, not as an argument to `nh task add`.)

**Who it's for:** engineers sitting on well-scoped work — bug fixes, test gaps,
small features — who would rather review a diff than write it.
**Who it isn't for:** anyone who wants an agent that ships to production
unattended. That is deliberately not built here.

---

## Table of Contents

- [Why no_human?](#-why-no_human)
- [Getting Started](#%EF%B8%8F-getting-started)
- [What You Get](#-what-you-get)
- [How It Works](#%EF%B8%8F-how-it-works)
- [CLI Reference](#-cli-reference)
- [Configuration](#-configuration)
- [Architecture](#%EF%B8%8F-architecture)
- [Development](#%EF%B8%8F-development)
- [Troubleshooting](#-troubleshooting)
- [FAQ](#-faq)
- [License](#-license)

---

## 🌟 Why no_human?

Plenty of tools will write code for you. The hard part is knowing whether to
trust the result. Three choices here follow from that:

- **The reviewer is adversarial, and it is not the author.** A fresh-context
  [Claude Agent SDK](https://docs.anthropic.com/en/docs/agents-sdk) session on a
  different model is told to *refute* "done" and must cite file:line or command
  output for every claim. There is no "score yourself 1–10" gate anywhere — a
  model grading its own work is not evidence.
- **Giving up honestly is a success.** When a task needs credentials, a missing
  system, or a decision that is genuinely yours, it parks with a structured
  blocker instead of inventing a plausible diff. Fabricated confidence costs far
  more to review than an honest escalation.
- **Trust only verifiable signals.** A net drop in test count or assertions is
  blocked *before* a reviewer token is spent, and a Python bugfix must ship a
  test that provably fails on the unfixed code.

Everything runs on your existing Claude subscription — no metered API bills, no
new vendor. One command boots the whole thing:

```bash
nh start   # web board + task worker + wake watcher
```

### Does it work?

There is a benchmark that replays real past tasks through the real pipeline and
scores the result against what the human actually did. The published run is
committed at [docs/NORTH_STAR_BENCH.md](docs/NORTH_STAR_BENCH.md): label
`expanded-core-v13`, 2026-07-21, a 56-spec run. It scored the goal satisfied
unattended on **25/53** runnable tasks (47%) — of which **15 delivered a change** and 10 correctly escalated — with an honest-escalation rate of
**77%** (10/13) on the tasks that were *expected* to escalate.

Everything about that needs qualifying, so here it is:

- **It is not the newest code, and its corpus is gone.** v13 ran BEFORE the
  vendor-neutral scrub that rewrote every spec's repo path, so it measured real
  repos — which is exactly why it is the run worth publishing, and also why it
  cannot be reproduced against the specs shipping here today. Six specs it
  scored are no longer curated; five current specs it never ran.
- **A single run is a point estimate.** Success moves several points between
  runs on identical specs, because the coder is non-deterministic. Read 47% as
  a range. The stable signals are cost and honest-escalation.
- **The cost figure is narrower than it looks.** The median cost ratio is
  0.1065, but the median covers only the **12 of 53** ran tasks where an
  original human-session cost was recorded at all, and no_human's side excludes
  planner and supervisor burn — so it UNDERSTATES what no_human really spent.
  "Cheaper" is well supported; any precise multiple is not.
- **"Follow-ups avoided" is smaller than the raw number.** 99 of them were
  earned on tasks no_human actually delivered. A further 251 came from tasks it
  correctly *escalated* — the right outcome, but the human still has to do that
  work, so those are not savings.
- **One spec of 53 burned zero tokens**, meaning it measured nothing; and the
  run is a 56-of-57 resume checkpoint, so one spec never reported at all.
- **It is self-run, not independent**, and you cannot reproduce it: the specs
  replay the author's own history and pin to local repo paths, so `nh bench run`
  will skip them on your machine. The harness is reusable; the corpus is not.
  `nh bench run` records its own `eval/results/northstar/<label>-<stamp>.json`
  and changes nothing else; `docs/NORTH_STAR_BENCH.md` moves only when someone
  runs `nh bench publish <results-file>`, which refuses a run too small or too
  quota-starved to mean anything. The figures above describe the run committed
  here.

The number that matters most is not the success rate anyway — it is that the
failures are mostly *honest* ones that cost you a minute to triage, rather than
a confident wrong diff that costs you an hour.

---

## ⬇️ Getting Started

**Prerequisites:** Python 3.12+, [uv](https://github.com/astral-sh/uv), git, the
[Claude CLI](https://claude.com/claude-code) (`npm install -g
@anthropic-ai/claude-code` — `claude setup-token` is how you mint the token), and a
Claude OAuth token — from either a paid personal subscription or an enterprise
profile (whatever `claude setup-token` issues for your plan). Both are
first-class; `nh auth use <profile>` picks which one pays, and a single run never
spans two profiles. What you cannot use is a metered `ANTHROPIC_API_KEY`:
startup aborts if one is set (see [Safety](#-safety-guarantees)).

```bash
git clone <your-clone-url>/no_human.git && cd no_human
uv sync
uv run nh init
```

`uv sync` installs the `nh` entry point into the project's `.venv`, so prefix
commands with `uv run` (or activate the venv) unless you have installed no_human
onto your `PATH` separately. Every `nh …` below assumes one of those.

`nh init` walks you through token setup, config generation, and first-repo onboarding — about 2 minutes.

Then add your first task:

```bash
nh task add --title "Add greet(name)" --repo ~/my-repo
```

The agent will plan, implement, get reviewed, run tests, and open a PR — all on your subscription token. See [docs/quickstart.md](docs/quickstart.md) for the full 5-minute walkthrough.

---

## 🎁 What You Get

| Feature | Description |
|---|---|
| **End-to-end delivery** | Plan → implement → adversarial review → test → PR. No manual steps except the final merge. |
| **Multi-source intake** | GitHub Issues, GitLab Issues, or freeform `--title`; Jira via an opt-in JQL poller. |
| **Independent reviewer** | A separate model instance told to *refute* "done" — evidence-cited checklist, never a self-score. |
| **Tamper guard** | Deterministic check: any net reduction in test count / assertions is blocked before review. |
| **CI integration** | Opt-in trigger + poll for GitLab CI, GitHub Actions, Jenkins or CircleCI, with infra-vs-real failure discrimination and auto-retry. |
| **Web board** | React operator terminal: a 3-column attention board (Needs Answer · Working · Review PR) with Failed/Done as outcome tables, a native unified diff view, approve / send-back. |
| **Blocker handling** | 10-category structured blockers with wake conditions, auto-resume, and escalation reports. |
| **Learning queue** | Mines your IDE sessions for reusable skills and anti-patterns. Nothing activates without your confirm. |
| **Eval harness** | Golden task replay, held-out tests, intent-match judge, red-team suite, CI regression gate. |
| **Shadow mode** | Full end-to-end run in a sandbox clone. Never pushes. |

---

## ⚙️ How It Works

```
Task in ──► Context ──► Plan ──► Implement ──► Review ──► Test ──► PR ──► You merge
              │                     │            │          │
              │                     │            │          └── local + CI
              │                     │            └── fresh-context adversarial reviewer
              │                     └── Claude Agent SDK (your subscription)
              └── codebase grep, git log, Teams, Outlook, session memory
```

**The agent never merges.** Every PR stops at `awaiting_approval` for your decision.

### Pipeline stages

1. **Intake** — parse a GitHub issue / freeform title into structured acceptance criteria. An interactive grill refines ambiguous specs.
2. **Context** — read-only, parallel gathering from codebase, comms, and past sessions. Large chunks are distilled through a read-only sub-agent.
3. **Plan** — generate a plan constrained by confirmed rules.
4. **Implement** — a preflight check reviews the plan before the first edit, then a Claude Agent SDK session runs with deterministic guards: scope guard (warns on out-of-plan edits), forbidden-import check, dependency-diff check, per-edit lint feedback.
5. **Review** — a fresh-context reviewer on a different model reads the diff, runs tests, and produces an evidence-backed pass/fail checklist. Fails loop back to implement (up to `max_attempts`).
6. **Test** — local test runner + optional CI trigger. Infra failures auto-retry; real failures loop back.
7. **PR** — deterministic VCS: branch, commit under a `no_human` identity, push, open PR/MR. Then park for your approval.

### 🔒 Safety guarantees

These are **enforced in code**, not advisory:

- **OAuth auth only** — subscription or enterprise profile. Metered API keys are scrubbed from the process env on startup, and a present `ANTHROPIC_API_KEY` aborts the run rather than being silently ignored, so a misconfiguration can never quietly bill the metered API.
- **Deterministic VCS** — the orchestrator owns branching, committing and pushing. Merging a PR/MR (`gh pr merge`, `glab mr merge`, the REST merge endpoints), force-push and destructive git are denied at the tool boundary, and `never_push_to` blocks pushes to protected branches.
- **Read-only review** — the reviewer backend blocks all write tools unconditionally.
- **Tamper guard** — fires before the reviewer; test-gutting agents are escalated immediately.
- **Reviewer crash → fail-closed** — no silent pass-through.

See [docs/security.md](docs/security.md) for the full model.

---

## 🚀 CLI Reference

The daily workflow is just two commands: `nh start` and `nh task add`. Everything below is there when you need it.

### Core workflow

```bash
nh init                                           # first-time setup wizard
nh start                                          # board + worker (the only command you need)
nh task add https://github.com/org/repo/issues/42 --repo ~/repo
nh task add --title "Fix X" --repo ~/repo         # freeform
```

### Observability

```bash
nh task list          # board as a table
nh task show <id>     # requirements, attempts, evidence
nh status             # portfolio overview: needs-you / working / waiting / done
nh logs <id>          # attempt log: turns, spend vs burn, failures
nh diff <id>          # git diff for latest commit
nh review <id>        # adversarial reviewer's evidence checklist
```

> **`nh watch <id>` is not read-only.** It *runs* the task in a live TUI
> (`cli/tui.py` builds an orchestrator and awaits `run_task`). Use it to drive a
> task in the foreground — never as a viewer while `nh start` is running, or the
> same task executes twice.

### Human actions

```bash
nh approve <id>                     # record approval — YOU merge the PR
nh reject <id> --reason "..."       # send back with feedback
nh blocked                          # parked tasks + the one question each needs
nh reply <id> "the answer"          # answer + resume from checkpoint
nh unblock <id>                     # manually resume (or --fail to abandon)
```

### Learning & evaluation

```bash
nh learnings                        # review pending proposals
nh learnings --confirm <id>         # activate a rule (one-click gate)
nh rules list                       # confirmed rules
nh skills list                      # confirmed skills
nh eval --gate                      # golden-set replay + CI gate
nh shadow "title" --repo ~/repo     # sandbox run, never pushes
```

### Repo management

```bash
nh onboard ~/repo                   # derive + prove install/test/lint commands
nh onboard ~/repo --confirm         # confirm the proven profile
nh history                          # extract IDE conversation history
nh history --analyze                # mine corrections → propose learnings
nh config show                      # pretty-print config
```

---

## 🔧 Configuration

All config lives at `~/.no_human/config.yaml` (auto-generated by `nh init`).  
Secrets live in `~/.no_human/.env` (chmod 600, never in the repo).

| Setting | Default | What it does |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | *(required)* | Subscription auth for the Claude Agent SDK |
| `llm.primary_model` | `claude-sonnet-5` | The implementer (coder) model |
| `llm.review_model` | `claude-opus-4-8` | The adversarial reviewer (kept different from the implementer by convention; not enforced in code) |
| `server.port` | `8420` | Web board bind port |
| `bounds.max_attempts` | `3` | Max implement → review cycles in **one** loop. Every resume starts a fresh loop, so this is not a per-task cap — `bounds.lifetime_attempts` is. |
| `bounds.max_turns_per_attempt` | `500` | Max agent turns per attempt |

<details>
<summary><b>Advanced settings</b> (CI, concurrency, safety, blockers)</summary>

| Setting | Default | What it does |
|---|---|---|
| `concurrency.max_workers` | `2` | Concurrent task workers (each in its own worktree; requires `concurrency.enabled`) |
| `ci.enabled` | `false` | Master switch for remote CI trigger + poll |
| `ci.backend` | `gitlab` | `gitlab` · `github_actions` · `jenkins` · `circleci` |
| `ci.project` | *(none)* | Project path for the CI trigger (GitLab-style) |
| `git.never_push_to` | `[main, master, release/*]` | Protected branch patterns |
| `safety.forbidden_paths` | `[.env, *.pem, ...]` | Paths the agent cannot write to |
| `notifications.slack_webhook_url` | `null` | Write-only Slack alerts (null = log only) |
| `blockers.max_park_duration` | `48h` | Auto-escalate parked tasks after this |
| `blockers.wake_poll_interval` | `10m` | How often the watcher checks wake conditions |

See [docs/configuration.md](docs/configuration.md) for the full reference.

</details>

---

## 🏗️ Architecture

```
~/.no_human/
  .env                  # secrets (chmod 600)
  config.yaml           # settings
  no_human.db           # SQLite (WAL) — tasks, attempts, profiles, memories

src/no_human/         # (abridged — packages only)
  cli/                  # nh commands (Click)
  api/                  # FastAPI + WebSocket + React SPA
  agent/                # Claude Agent SDK backend, guards, hooks
  core/                 # orchestrator, state machine, scheduler, bounds
  intake/               # GitHub, GitLab adapters + Jira poller
  context/              # parallel read-only context gatherers
  review/               # adversarial reviewer
  testing/              # local test runner, test-layer model
  ci/                   # remote CI trigger + poll (opt-in)
  vcs/                  # deterministic git, PR opening
  learning/             # human-confirmed learning queue
  history/              # IDE transcript extraction + analysis
  eval/                 # golden-set replay + scorecard
  blockers/             # 10-category blocker taxonomy + wake watcher
  integrations/         # Jira / CI / notification integrations
  ci_gate/               # enterprise CI adapter (optional)
  notify/               # Slack webhook notifications
```

Design source of truth: [`PLAN.md`](PLAN.md). Implementation brief: [`BUILD.md`](BUILD.md).

---

## 🛠️ Development

```bash
uv sync                     # install dependencies
uv run pytest -q            # run all tests
uv run nh --help            # CLI reference
```

### E2E tests (web board)

```bash
uv sync --group e2e
uv run playwright install chromium
cd web && npm install && npm run build && cd ..
uv run python e2e/serve_demo.py 8488 &
NH_E2E_BASE=http://127.0.0.1:8488 uv run python e2e/board_e2e.py
```

### 📖 Key design documents

| Document | Purpose |
|---|---|
| [`PLAN.md`](PLAN.md) | Architectural spec and constraints |
| [`BUILD.md`](BUILD.md) | Implementation brief |
| [`EVOLUTION_PLAN.md`](EVOLUTION_PLAN.md) | Evolution roadmap |
| [`docs/security.md`](docs/security.md) | Safety model |
| [`docs/configuration.md`](docs/configuration.md) | Full config reference |
| [`docs/adapters.md`](docs/adapters.md) | Intake adapter setup (GitHub, GitLab) |
| [`docs/eval.md`](docs/eval.md) | Evaluation harness and golden tasks |

---

## ❓ Troubleshooting

| Problem | Solution |
|---|---|
| **`auth error: ANTHROPIC_API_KEY is set`** | `unset ANTHROPIC_API_KEY` — no_human runs on an OAuth token, never the metered API. Startup aborts rather than scrubbing silently, so the misconfiguration reaches you and not your bill. |
| **`auth error: No subscription token found`** | Run `claude setup-token`, then add the token to `~/.no_human/.env` (chmod 600). `nh auth status` lists configured profiles. |
| **`no profile to confirm`** | Run `nh onboard <repo>` first, then `nh onboard <repo> --confirm`. |
| **Task stuck in `implementing`** | Check `nh logs <id>` — likely hit `max_turns`. Raise `bounds.max_turns_per_attempt` (default 500) or simplify the task. |
| **`another no_human instance is already running`** | Only one instance may hold `~/.no_human/nh.pid`. There is no `nh stop`: stop the process that holds it (Ctrl-C in its terminal, or signal the PID inside that file). If it died uncleanly, `rm ~/.no_human/nh.pid`. |
| **Reviewer keeps failing** | Check `nh review <id>` for evidence. Either the code has real issues (fix and retry) or the acceptance criteria are ambiguous (refine with `nh reject`). |

---

## 💬 FAQ

<details>
<summary><b>Does this use my personal API key?</b></summary>

No. no_human runs only on an OAuth token (`CLAUDE_CODE_OAUTH_TOKEN`) — from a personal subscription or an enterprise profile. Metered API keys are scrubbed from the environment on startup, and if `ANTHROPIC_API_KEY` is set the process refuses to start (a silent scrub-and-continue would hide a misconfiguration that costs real money). Your subscription covers the cost.
</details>

<details>
<summary><b>Can the agent merge a PR?</b></summary>

No. The pipeline stops at `awaiting_approval`. There is no auto-merge anywhere in the codebase. You review the diff and the evidence checklist, then merge — in your git host, or with `nh merge-stack run` for a stacked set. Both are operator actions; the agent is denied the merge tools outright.
</details>

<details>
<summary><b>What if the agent gets stuck?</b></summary>

The orchestrator has built-in stuck detection: the same error signature twice triggers a context reset. After `max_attempts` (default 3) the loop escalates with a structured report. A resume starts a fresh loop, so the per-task ceiling is `bounds.lifetime_attempts`, not this. You can also `nh blocked` to see what's parked and `nh reply` to provide guidance.
</details>

<details>
<summary><b>Can I use this for multiple repos?</b></summary>

Yes. Create a project with multiple repo paths. The test-plan model supports cross-repo test execution — e.g. unit tests in the primary repo, integration tests in a separate test repo. The write path covers all repos: each linked repo gets its own branch, commit, and PR (linked back to the primary). A single `nh approve` covers all PRs. The tamper guard runs on every repo — test-gutting in a linked repo is caught and escalated.
</details>

<details>
<summary><b>How does learning work?</b></summary>

Success → proposed skill. Structural blocker → proposed anti-pattern. The agent mines your IDE conversations for correction patterns. **Nothing enters the active rule set without your explicit confirm** (`nh learnings --confirm`). This prevents one-off context from calcifying into permanent rules.
</details>

---

## 📄 License

MIT — see [LICENSE](LICENSE) for details.

---

## ✍️ Author

Created by **eyalgolan** — exploring what autonomous software delivery looks like when you refuse to compromise on trust, safety, or human oversight.

## 💭 Feedback & contributions

Found a bug? Have an idea? Open an issue or start a discussion. PRs are welcome — run the test suite (`uv run pytest -q`) before submitting.

---

<div align="center">
<sub>Built for engineers who'd rather review code than write boilerplate.</sub>
</div>
