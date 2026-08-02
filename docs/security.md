# Security & safety model

no_human is designed for **unattended** runs, so its safety properties are
correctness requirements, not preferences. They are enforced in code and covered
by tests.

## 1. One billing path per run

Two sanctioned auth modes exist. The default, `llm.auth_mode: "subscription"`,
runs on a Claude OAuth token (personal or enterprise, from
`claude setup-token`); `llm.auth_mode: "api_key"` is an operator opt-in that
bills the operator's own Anthropic account with their own `ANTHROPIC_API_KEY`.
The Claude Agent SDK honours `ANTHROPIC_API_KEY` **over**
`CLAUDE_CODE_OAUTH_TOKEN` when both are present, so in either mode startup
(`config.assert_subscription_mode`) guarantees a run bills exactly the one
configured path:

1. Every auth variable that could redirect billing elsewhere is **scrubbed**
   from the process environment: `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`,
   the Bedrock/Vertex set, `GOOGLE_APPLICATION_CREDENTIALS`,
   `AWS_BEARER_TOKEN_BEDROCK` — and, in the default mode, `ANTHROPIC_API_KEY`.
2. In the default mode, if `ANTHROPIC_API_KEY` was present the process
   **refuses to start** (exit 2) — the scrub already protected this run, but
   startup aborts so you fix the source rather than masking a misconfiguration.
3. The credential for the configured mode (OAuth token, or API key in
   `api_key` mode) is loaded from `~/.no_human/.env`
   (`chmod 600`, gitignored, **never** in the repo) with a process-env fallback.

Credentials are never read from or written to anywhere in the repo, and
`ANTHROPIC_API_KEY` is rejected if it appears in `config.yaml` — the *mode*
may live in config; the key never does.

## 2. The agent never merges

The orchestrator opens a PR/MR and **stops** at `awaiting_approval`. There is no
auto-merge anywhere; merge is always a human action. The board's **Approve**
button records approval and tells you to merge in your git host — it does not
merge. `approval.auto_merge_on_approval` is hard-wired `false`.

## 3. Deterministic VCS under a distinct identity

Git is owned by the orchestrator, never the LLM: branch, commit (as
`no_human <no-human@acme.com>`, distinct from you), push, open PR. The
PreToolUse guard blocks `git merge`, force-push, `rm -rf`, and writes to
`forbidden_paths`. `never_push_to` (`main`, `master`, `release/*`) is refused at
the git layer. During review the backend runs **read-only**: all write tools are
blocked unconditionally.

## 4. Trust only verifiable signals

- **Tamper guard**: any net reduction in test count / assertions between the base
  and the change is blocked *before* the reviewer runs (cheap, deterministic).
- **Independent reviewer**: a fresh-context `claude-opus-5` subagent told to
  refute "done", producing an evidence-cited pass/fail checklist — **never a
  numeric self-score**. Reviewer crash → fail-closed.
- **Held-out tests**: `tests/held_out/` are run by the orchestrator and given to
  the reviewer as evidence the implementer never saw.
- **CI retry** only on infra failures (max 2); real failures never auto-retry.

Each of those gates, with the code that enforces it and what it does *not*
cover: [verification.md](verification.md).

## 5. Bounded loop + honest blockers

`max_attempts`, per-attempt `max_turns`, and stuck detection (same error
signature twice → reset context, don't stack corrections). A blocker is **never**
resolved by weakening tests, expanding scope, editing acceptance criteria, or
faking done — the agent makes verifiable progress, parks with a wake condition,
or escalates with a structured report (see [blockers.md](blockers.md)).

## 6. What to review before trusting an unattended run

- `~/.no_human/.env` is `chmod 600` and holds the credential for the
  configured mode (`CLAUDE_CODE_OAUTH_TOKEN`, or `ANTHROPIC_API_KEY` in
  `api_key` mode), alongside any integration secrets (Jira, CircleCI...).
- `nh eval --gate` is green and the red-team suite shows zero tamper / faked-done
  incidents.
- `never_push_to` and `forbidden_paths` match your repo's protected surface.

## 7. What leaves your machine

**Read this first: no_human is not an offline tool, and this page does not claim
to be an exhaustive list of its network traffic.** It cannot be one. The coder
session is a Claude Agent SDK session built with **no tool restrictions** and
`permission_mode="bypassPermissions"` (`agent/claude_backend.py:309`, `:423`) —
no tool allowlist, no tool denylist, no per-call permission callback. It has
Bash. Anything an agent decides to run — `curl`, `pip install`, `npm i`, a test
suite that hits a staging API — leaves your machine, and nothing in no_human sits
between it and the network. An exhaustive egress claim cannot survive that, so
this page does not make one. If that is unacceptable for your codebase, the
control is the machine (a container, a network policy, an egress proxy), not a
config key here.

What follows is the traffic that is **ours** — deliberate, in our code, and the
part we are accountable for. It is split by whether it happens on a default
install or only after you configure it; "configuration-required" below always
means the default in `config.DEFAULT_CONFIG` is off/empty, never merely that a
key exists. `tests/test_egress_disclosure.py` fails if a module in
`src/no_human/` gains an outbound HTTP client or a network CLI call and is not
named here.

### On by default

- **Prompts to Anthropic**, on your own credential (`llm.auth_mode`). Source
  files, diffs, test output and ticket text go into these. This is the point of
  the tool.
- **`git push` of the task branch to your git remote**,<!-- egress:push --> followed by opening a
  pull request (`GitRepo.push` in `vcs/git.py`, via `open_pr` in
  `vcs/__init__.py`, called from `Orchestrator._finalize` in
  `core/orchestrator.py`). <!-- egress:push:no-optout -->**This ships your source to your git host, and
  there is no key that disables it**<!-- /egress:push:no-optout --> — a task's deliverable IS the PR.
  `never_push_to` (default `main`, `master`, `release/*`) chooses **where** a
  push may land, never **whether** one happens; a branch it protects is refused
  at the git layer (`ProtectedBranch`), and the agent is denied `gh pr merge` /
  `glab mr merge` regardless.<!-- /egress:push -->
- **PR body, and review comments that quote your code**, posted through `gh` /
  `glab` (`vcs/github.py`, `vcs/gitlab.py`, `vcs/comment_poster.py`). The PR
  body carries the commit summary and test evidence; reviewer findings cite file
  and line and quote the lines they are about. Same destination as the push.
- **PR receipt and status polling** — `gh` / `glab` calls for the PR's head SHA
  and its mergeability (`vcs/pr_watcher.py:507-533`, `vcs/receipts.py`), plus
  `git fetch origin` (`vcs/git.py:374-381`), while a task waits on CI or review.
  These read; they send only the identifiers of a PR you just created.
- **`nh merge-stack run` calls `gh pr merge`** against your git host
  (`cli/commands.py:1661`). This is *your* command, not the agent's — the agent
  is never allowed to reach it (§2).
- **One `GET https://pypi.org/pypi/no-human/json` per day**, to notice a newer
  release (`updates.py:39`). No identifier, no repo name, no telemetry — the
  same request `pip install` makes. On by default (`updates.enabled: true`,
  `interval_seconds: 86400`); off with `updates.enabled: false` in
  `~/.no_human/config.yaml` or `NH_NO_UPDATE_CHECK=1`
  (`updates.py:52`, which also covers CI).
- **The desktop app checks GitHub Releases at startup**, once a day
  (`desktop/main.mjs:612` → `desktop/updater.mjs:104`, feed
  `provider: github, owner: eyalgolan, repo: no_human` —
  `desktop/electron-builder.config.cjs:82`). It never downloads on its own
  (`autoDownload` is off, `desktop/updater.mjs:66`). **This is a separate code
  path from the PyPI check above and neither `NH_NO_UPDATE_CHECK` nor
  `updates.enabled` exists in `desktop/` — those switches do not reach it.**
  Today the only way to stop it is to not run the desktop app. That gap is a
  defect, not a design.

### Only if you configure it

Every channel below sends nothing at all on a default install. Each names the
config key that turns it on and the default that keeps it off.

- **Your CI provider.** `ci.enabled` defaults to **`false`**, and with it off
  `ci_from_config` returns `None` and no backend is ever constructed. Every
  backend additionally needs a `ci.project`/`ci.job`/`ci.repo`; enabled with
  none of them, `ci_from_config` raises `CIMisconfigured` and the run
  escalates rather than proceeding ungated — still no backend, still nothing
  sent anywhere (`ci/__init__.py`).
  Once you enable one:
  - **GitLab CI** (`ci.backend: "gitlab"`, the default *choice* but not a
    default *state*) POSTs a pipeline via
    `glab api --method POST projects/<project>/pipeline`, sending the **branch
    name** and the **key/value pairs you put in `ci.variables`** (default `{}`)
    as the request body (`ci/gitlab.py:155-172`). It has no watch-only mode: if
    it is enabled, it triggers.
  - **Jenkins** (`ci.backend: "jenkins"`) reaches `ci.base_url` over `curl`
    (`ci/jenkins.py:301-330`). `ci.mode` defaults to **`watch`**, which only
    polls `…/lastBuild/api/json`; **`ci.mode: "trigger"` is opt-in** and POSTs
    `…/buildWithParameters` with `ci.variables` in the query string
    (`ci/jenkins.py:154-169`). The same job API is used by the PR-image
    enrichment path (`ci_gate/enrich.py:70-83`).
  - **CircleCI** (`ci.backend: "circleci"`) talks to
    `https://circleci.com/api/v2` with the `CIRCLECI_TOKEN` from
    `~/.no_human/.env`. `ci.mode` defaults to **`watch`** — a `GET` of the
    pipeline your PR push already started (`ci/circleci.py:169-180`).
    **`ci.mode: "trigger"` is opt-in** and sends a JSON POST
    `{"branch": "<your branch>"}` to `POST /project/<slug>/pipeline`
    (`ci/circleci.py:182-186`).
  - **GitHub / GHE check runs** (`ci.backend: "github_actions"` or
    `"ghe_checkruns"`) read status via `gh` and never write
    (`ci/ghe_checkruns.py`, `ci/github_actions.py`).
- **Ticket trackers**, for intake and optional write-back:
  `integrations.jira.enabled` and `integrations.linear.enabled` both default to
  **`false`** and hold no credential (`intake/jira.py`, `intake/linear.py` →
  `https://api.linear.app/graphql`). GitHub/GitLab issue intake goes out over
  `gh` / `glab` and only runs for a ticket ref you hand it
  (`intake/github_issues.py`, `intake/gitlab_issues.py`).
- **Microsoft Graph (Teams + Outlook context).** If you configure
  `context.m365.token`, your query text is sent to Microsoft Graph — a POST to
  `https://graph.microsoft.com/v1.0/search/query` carrying the task's external
  ID or up to three keywords derived from the task title
  (`context/teams.py:35`, `:55-66`; `context/outlook.py` shares the same
  client). `DEFAULT_CONFIG` has no `context.m365` block at all, so with no token
  configured the client **fails closed and sends nothing** — it raises before
  building the request (`context/teams.py:50-54`). It is opt-in, not default-on.
- **Slack / Teams notification webhooks.** `notifications.slack_webhook_url` and
  `notifications.teams_webhook_url` both default to **`null`**; with a URL set,
  a task-status line (and, for Teams, a card linking to your board) is POSTed to
  it (`notify/slack.py:53`, `notify/teams.py:205`).
- **Integration health checks.** `nh integrations` / the board's integrations
  page authenticate against whichever of Jira, Linear, CircleCI and the Teams
  webhook you have configured, to show a live status
  (`integrations/__init__.py:497`, `:533`). Nothing configured → nothing sent.
- **Team brain control plane.** `team_brain.enabled` defaults to **`false`** and
  `team_brain.control_plane_url` to **`""`**; when set, the client exchanges
  task patterns with that URL over `https` (loopback excepted)
  (`brain/client.py:89-133`).

### Not egress: loopback

The CLI, the desktop app and the MCP bridge talk to no_human's **own** API on
`127.0.0.1:8420` (`cli/api_client.py`, `intake/mcp_bridge.py:29`,
`cli/commands.py:64-70`), and the transcript-research reader probes a language
server on localhost (`history/extractor.py:65-72`). These never leave the
machine, and `server.host` defaults to `127.0.0.1`.

### What you actually control

| | |
|---|---|
| Which credential pays, and that only one does | `llm.auth_mode`, `nh auth use <profile>` |
| Which branches can never be pushed to | `git.never_push_to` |
| Which paths the agent may not touch | `forbidden_paths` |
| Whether the CLI checks PyPI | `updates.enabled`, `NH_NO_UPDATE_CHECK=1` |
| Whether any CI provider is contacted | `ci.enabled` (**off** by default) |
| Whether a CI run is *triggered* rather than watched | `ci.mode` (Jenkins, CircleCI); GitLab always triggers |
| Whether your query text reaches Microsoft | `context.m365.token` (**unset** by default) |
| Whether notifications leave the machine | `notifications.*_webhook_url` (**null** by default) |
| Whether a ticket tracker is contacted | `integrations.jira.enabled`, `integrations.linear.enabled` (**off**) |
| Whether a PR is pushed at all | **nothing — it always is** |
| Whether the desktop app checks for updates | **nothing — don't run it** |
| What else the coder session may reach | **nothing in-process — use the OS** |

### History

Until 2026-08-01 this page and the README said prompts were the only thing that
left the machine. That was false when written: `open_pr` terminates every task.
The first correction added the PyPI check and repeated the false claim in a new
form ("two things, and nothing else"), which was worse — it read as an audited
enumeration. The lesson is recorded here rather than quietly fixed: **an
exhaustive claim about egress cannot be made about a process that gives an agent
an unrestricted shell.** Name the channels that are yours, name the unbounded
one, and let the reader decide.
