# Quickstart — from zero to first task in 5 minutes

## Installed the Windows app? Start here instead

**If you ran `no_human-<version>.exe`, sections 1–3 below are NOT for you.** The
same reasoning as the Mac section that follows: the app carries its own Python,
its own server and its own dependencies, so there is nothing to install, nothing
to clone, and no `uv sync` to run. Doing any of it would set up a *second*,
unrelated copy.

Two things specific to Windows, both expected:

* The installer is **not code-signed yet**, so if you downloaded it, Windows
  SmartScreen says *"Windows protected your PC"*. Choose **More info → Run
  anyway**. Signing is planned; until then this prompt is normal.
* It installs **per user** — no administrator prompt — into
  `%LOCALAPPDATA%\Programs\no-human-desktop`. The folder is named after the
  package while the app itself is called **no_human**; that is normal.

What you actually do:

1. Open **no_human** from the Start Menu.
2. It shows **Connect Claude** and asks for a credential — either a Claude
   subscription token (it looks like `sk-ant-oat…`) or an Anthropic API key.
   Paste one and continue.
3. The board opens. Create your first task there.

Then confirm the install is actually working:

```powershell
& "$env:LOCALAPPDATA\Programs\no-human-desktop\resources\nh-server\nh.exe" doctor
```

Expect a `coding backend` line, a mechanism-liveness table, and
`no contradictions, no evidence gaps` with exit code 0. Same command, same
output and same exit codes as the Mac install — only the path to the bundled
binary differs. See
[INSTALLER.md#verify-your-install-is-real](INSTALLER.md#verify-your-install-is-real),
and `WINDOWS.md` for the Windows build and its known limits.

To uninstall: **Settings → Apps → no_human**. Your tasks and credential live in
`~/.no_human` and are deliberately left behind, so reinstalling picks up where
you left off; delete that folder yourself if you want a clean slate.

## Installed the Mac app? Start here instead

**If you opened a `.dmg` and dragged no_human to Applications, sections 1–3
below are NOT for you.** The app carries its own Python, its own server and its
own dependencies — there is nothing to `brew install`, nothing to clone, and no
`uv sync` to run. Doing any of it would set up a *second*, unrelated copy. The
Connect Claude screen below does what `nh init` (section 3) does for a source
install: it writes your credential to `~/.no_human/`.

What you actually do:

1. Open **no_human** from Applications.
2. It shows **Connect Claude** and asks for a credential — either a Claude
   subscription token (it looks like `sk-ant-oat…`) or an Anthropic API key.
   Paste one and continue.
3. The board opens. Create your first task there.

If you ever need that screen again — a revoked or mistyped token strands the
app otherwise — it is **File → Re-enter Claude Token…**.

Then confirm the install is actually working — the same liveness check
section 3 points source installs to, reachable without one:

```bash
/Applications/no_human.app/Contents/Resources/nh-server/nh doctor
```

`nh doctor` is a subcommand of the binary the app already bundles, not
something the source install adds — see
[INSTALLER.md#verify-your-install-is-real](INSTALLER.md#verify-your-install-is-real)
for expected output (both a healthy and a failing run) and troubleshooting.

Everything from section 4 onward applies to you too, EXCEPT that commands are
written as `uv run nh …` for the source install. The packaged app runs the same
server internally, so use the board rather than the CLI unless you have also
installed from source.

---

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

`uv sync` installs the `nh` entry point into `.venv/bin/nh`. It does **not** put
`nh` on your `PATH`, so every command below is written as `uv run nh …`. If you
would rather type a bare `nh`, either install it as a tool —

```bash
uv tool install --editable .      # then `nh` works from anywhere
```

— or activate the venv once per shell (`source .venv/bin/activate`) and drop
the `uv run` prefix everywhere.

## 3. Run `nh init`

```bash
uv run nh init
```

`nh init` is an interactive wizard: it asks how you want to pay for Claude and
walks you through the token. There is currently no non-interactive/`--yes` mode,
so it cannot be run from a provisioning script or over a pipe — see
[docs/KNOWN_ISSUES.md](KNOWN_ISSUES.md).

This guided wizard will:
- Check that python, git, uv, and claude CLI are installed
- Create `~/.no_human/` with secure permissions
- Guide you through subscription token setup (`claude setup-token`)
- Generate `~/.no_human/config.yaml` with a distinct agent git identity
  (your own identity is read and shown, but the agent commits under its own)
- Offer to onboard your first repo

Then confirm the install is actually working before you rely on it:

```bash
uv run nh doctor
```

`nh doctor` is a liveness check, and it answers a question no other command
does: **which guarded mechanisms have actually ever fired.** It enumerates every
mechanism's lifetime firings, flags the known silent-death patterns (a gate that
has never run, a watcher that has persisted nothing), reports your auth profile
and mode, and refuses if the coding backend is unusable. It exits non-zero on a
contradiction or an evidence gap, so `nh doctor || exit 1` works in a pipeline.

On a brand-new install most counters will read zero, which is expected — nothing
has run yet. Its value is later: run it whenever something behaves oddly, and
paste it into any bug report.

## 4. Add your first task

```bash
# From a freeform title (works with no tracker at all):
uv run nh task add --title "Fix the flaky E2E test" --repo ~/git/my-repo \
  --description "..." --criteria "the test passes 20 runs in a row"

# From a GitHub or GitLab issue URL:
uv run nh task add https://github.com/org/repo/issues/42 --repo ~/git/my-repo
```

`nh task add` takes **a GitHub/GitLab issue URL, or `--title`**. A bare
ticket key such as `PROJ-42` is *not* a supported argument: `ingest_from_url`
raises, the CLI prints `intake failed: not a recognized task URL/id` and exits
1 — the standalone tracker adapter that once accepted it has been removed. Use
`--title` if you want that text as a freeform task. Jira issues come in through
the **poller** instead, not through `nh task add`; see
[adapters.md](adapters.md#jira) for the `integrations.jira` config block.

## 5. Run one in the foreground

```bash
uv run nh watch <task-id>
```

This opens a live Textual TUI showing tool calls, agent reasoning, and progress.

> ⚠️ Despite the name, `nh watch` **runs** the task in a foreground TUI — it is
> not a read-only viewer (`cli/commands.py`: "Run a staged task in the live
> Textual TUI"). Point it only at a *staged* task. Do **not** point it at one
> that `nh start`'s worker is already working, or the task runs twice. To just
> look at a running task, use `uv run nh status`, `uv run nh logs <task-id>`,
> or the web board.

## 6. Check on tasks

```bash
uv run nh task list          # board as a table
uv run nh blocked            # parked/escalated tasks + what each needs
uv run nh status             # portfolio overview
```

`nh dashboard` is an alias for `nh start`: it also starts a task worker and
the wake watcher, so it *runs* queued work rather than just showing it. To only
look, use `nh status` / `nh logs`, or open the board a running server already
serves.

## 7. Review and approve

When a task produces a PR:

```bash
uv run nh review <task-id>   # evidence-backed review checklist
uv run nh diff <task-id>     # the git diff
uv run nh approve <task-id>  # record your approval — YOU merge the PR
```

If you want changes:

```bash
uv run nh reject <task-id> --reason "The error handling needs a retry"
```

## 8. Overnight drain (parallel)

Queue up several tasks with `--no-run` so they stage as PENDING instead of
running immediately, then drain them all in one bounded pool:

```bash
uv run nh task add --title "Fix the flaky E2E test" --repo ~/git/my-repo --no-run
uv run nh task add --title "Add input validation to isqrt" --repo ~/git/my-repo --no-run
uv run nh task add https://github.com/org/repo/issues/42 --repo ~/git/my-repo --no-run

uv run nh serve --max-workers 3
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

**`auth error: No subscription token found. Expected CLAUDE_CODE_OAUTH_TOKEN in …`**
→ Run `claude setup-token`, then add the token to `~/.no_human/.env`

**`no profile to confirm`**
→ Run `uv run nh onboard <repo>` first, then `uv run nh onboard <repo> --confirm`.
If the proving step prints `[FAILED] test: … (exit N)`, run that command yourself
in the repo to see the real error — onboarding does not yet show it.

**`intake failed: not a recognized task URL/id`**
→ `nh task add` takes an issue URL or `--title "…"`. A bare tracker key is not
an accepted argument; see step 4.

**`nh: command not found`**
→ `uv sync` installs `nh` into `.venv`, not onto your `PATH`. Use `uv run nh …`,
or `source .venv/bin/activate` once per shell.
