# Quickstart — from zero to first task in 5 minutes

## 1. Install prerequisites

```bash
# macOS
brew install python@3.12 uv git
npm install -g @anthropic-ai/claude-code   # for the Claude CLI

# Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# ensure python 3.12+ and git are installed via your package manager
npm install -g @anthropic-ai/claude-code
```

## 2. Clone and install no_human

```bash
git clone <repo_url> no_human && cd no_human
uv sync
```

## 3. Run `nh init`

```bash
nh init
```

This guided wizard will:
- Check that python, git, uv, and claude CLI are installed
- Create `~/.no_human/` with secure permissions
- Guide you through subscription token setup (`claude setup-token`)
- Generate `~/.no_human/config.yaml` with your git identity
- Offer to onboard your first repo

## 4. Add your first task

```bash
# From a TRACKER story/defect number:
nh task add PROJ-42 --repo ~/git/my-repo

# From a freeform title:
nh task add --title "Fix the flaky E2E test" --repo ~/git/my-repo

# From a GitHub/GitLab URL:
nh task add https://github.com/org/repo/issues/42 --repo ~/git/my-repo
```

## 5. Run one in the foreground

```bash
nh watch <task-id>
```

This opens a live Textual TUI showing tool calls, agent reasoning, and progress.

> ⚠️ Despite the name, `nh watch` **runs** the task in a foreground TUI — it is
> not a read-only viewer (`cli/commands.py`: "Run a staged task in the live
> Textual TUI"). Point it only at a *staged* task. Do **not** point it at one
> that `nh start`'s worker is already working, or the task runs twice. To just
> look at a running task, use `nh status`, `nh logs <task-id>`, or the web
> board.

## 6. Check on tasks

```bash
nh task list          # board as a table
nh blocked            # parked/escalated tasks + what each needs
nh status             # portfolio overview
nh dashboard          # open the web board in your browser
```

## 7. Review and approve

When a task produces a PR:

```bash
nh review <task-id>   # evidence-backed review checklist
nh diff <task-id>     # the git diff
nh approve <task-id>  # record your approval — YOU merge the PR
```

If you want changes:

```bash
nh reject <task-id> --reason "The error handling needs a retry"
```

## 8. Overnight drain (parallel)

Queue up several tasks with `--no-run` so they stage as PENDING instead of
running immediately, then drain them all in one bounded pool:

```bash
nh task add --title "Fix the flaky E2E test" --repo ~/git/my-repo --no-run
nh task add --title "Add input validation to isqrt" --repo ~/git/my-repo --no-run
nh task add PROJ-42 --repo ~/git/my-repo --no-run

nh serve --max-workers 3
```

`--max-workers N` runs the pool for this invocation even if
`concurrency.enabled` is `false` in `config.yaml` — no config edit needed.
Every task — one at a time or many — runs isolated in its own git
**worktree** (`isolation.enabled`, on by default), so a run never touches the
checkout you are working in, and parallel tasks never share a checkout or stomp
each other's branch. Isolation is a separate switch from parallelism: turning
it off (`isolation.enabled: false`) puts the agent in your primary checkout,
and a pool wider than one worker is then refused outright. Leave `nh serve` running
overnight; wake up to open PRs and review with `nh review` / `nh approve` as
in step 7 — **merge always stays a human action**, `nh serve` never merges.

---

## Key files

| Path | Purpose |
|------|---------|
| `~/.no_human/.env` | Secrets (chmod 600): subscription token, CI tokens |
| `~/.no_human/config.yaml` | Configuration: models, git, safety, bounds |
| `~/.no_human/no_human.db` | SQLite database: tasks, attempts, profiles |
| `<repo>/.no_human/project.yml` | Per-repo profile (test/lint/CI commands) |

## Troubleshooting

**`auth error: ANTHROPIC_API_KEY is set`**
→ The default subscription mode scrubs a stray `ANTHROPIC_API_KEY` and aborts
startup so a run bills exactly one path. `unset ANTHROPIC_API_KEY`, or opt in
to `llm.auth_mode: api_key` to bill that key deliberately.

**`auth error: CLAUDE_CODE_OAUTH_TOKEN not found`**
→ Run `claude setup-token`, then add the token to `~/.no_human/.env`

**`no profile to confirm`**
→ Run `nh onboard <repo>` first, then `nh onboard <repo> --confirm`

**`inbox unavailable`**
→ Configure TRACKER credentials in `~/.no_human/.env` (see `docs/adapters.md`)
