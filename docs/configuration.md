# Configuration

## Settings at a glance

The settings most installs touch. This table moved off the README on 2026-08-01
(the front page now links here instead of restating it); it is pinned to
`config.DEFAULT_CONFIG` by `tests/test_readme_claims.py`, so a default that
changes in code and not here fails the suite.

| Setting | Default | What it does |
|---|---|---|
| `llm.auth_mode` | `subscription` | `subscription` (OAuth) or `api_key` (your own key) |
| `llm.primary_model` | `claude-sonnet-5` | The implementer |
| `llm.review_model` | `claude-opus-5` | The fresh-context reviewer |
| `bounds.max_attempts` | `3` | Implement/review cycles in one loop |
| `bounds.max_turns_per_attempt` | `500` | Agent turns before an attempt is cut off |
| `server.port` | `8420` | Web board bind port |
| `concurrency.enabled` | `false` | Parallel task workers, each in its own worktree |
| `ci.enabled` | `false` | Trigger and poll GitLab CI, GitHub Actions, Jenkins or CircleCI |

Concurrency ships off, and `concurrency.max_workers` defaults to 2 when you turn
it on. In the default `subscription` mode a present `ANTHROPIC_API_KEY` aborts
startup rather than being silently ignored
([`assert_subscription_mode`](../src/no_human/config.py)) — silently scrubbing it would hide
a misconfiguration that costs real money. In `api_key` mode the reverse holds:
your key is the billing path and every *other* metered route is scrubbed, so a
run bills exactly one thing and records which.

Config lives at `~/.no_human/config.yaml`, auto-generated with defaults on first
run. The user's values are deep-merged over the defaults. The metered
`ANTHROPIC_API_KEY` must never appear here — in **every** `llm.auth_mode`,
including `api_key` mode (below), the key itself lives only in `~/.no_human/.env`,
never in `config.yaml` (loading rejects it). Only the auth *mode* is a config value.

Secrets live separately in `~/.no_human/.env` (`chmod 600`, gitignored). They are
loaded into the process env on startup, never read from or written to the repo,
and never logged or echoed.

## `~/.no_human/.env` keys

`nh onboard <repo>` derives which keys a given repo needs (from its CI backend +
VCS host) and prints a present/✗-missing checklist. When a task hits a missing
credential at runtime, no_human escalates a `MISSING_ACCESS` blocker naming the
**exact** key to set — set it, then `nh reply` to resume.

| Key | When you need it |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | Default `llm.auth_mode: subscription` — **required**, the coding backend's subscription auth. Create with `claude setup-token`. |
| `ANTHROPIC_API_KEY` | Only when `llm.auth_mode: api_key` — the operator's own metered key, BYO-API-key billing (see below). Never set otherwise. |
| `JIRA_API_TOKEN` | Jira intake (`integrations.jira.enabled: true`). An Atlassian Cloud API token; auth is HTTP Basic `integrations.jira.email` + this token. See [adapters.md](adapters.md#jira). |
| `LINEAR_API_KEY` | Linear intake (`integrations.linear.enabled`). Create at Linear → Security & access settings. |
| `JENKINS_USER`, `JENKINS_API_TOKEN` | Repos whose CI is Jenkins (`build.example.com`) or human-gated on a `Jenkinsfile`. Basic auth — the default `ci.auth: token` mode. |
| `SSO_USERNAME`, `SSO_PASSWORD` | Jenkins controllers that reject API-token basic auth, i.e. `ci.auth: cookie`. Used once to capture a session cookie. |
| `CIRCLECI_TOKEN` | `ci.backend: circleci`. A CircleCI personal API token; sent as the `Circle-Token` header. |
| `GITLAB_TOKEN` | Repos whose CI backend is GitLab, or whose VCS host is a GitLab. |
| `GH_ENTERPRISE_TOKEN` | Opening PRs against a GitHub-Enterprise host (e.g. `code.example.com`). Public `github.com` uses `gh auth login` instead. |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | Only for the opt-in Slack Socket-Mode **intake** worker (`integrations.slack.intake`). Unrelated to notify-out. |

> **The notify-out webhooks are NOT `.env` keys.** Slack's and Teams' webhook
> URLs live in `config.yaml` under `notifications.slack_webhook_url` and
> `notifications.teams_webhook_url` — that is where the code reads them from
> (`notify/build_notifier`). They are still treated as secrets: never echoed
> back by the settings UI, and scrubbed from `/api/config`. Earlier revisions of
> this table listed a `SLACK_WEBHOOK_URL` env var and a `TRACKER_TOKEN`; **no
> code has ever read either one.** They are removed rather than reworded.

In the default `subscription` mode, `ANTHROPIC_API_KEY` and any Bedrock/Vertex
var must **never** appear here — they are scrubbed on startup and a present
`ANTHROPIC_API_KEY` aborts the run (it would silently bill metered API). In the
opt-in `api_key` mode, `ANTHROPIC_API_KEY` is instead the one **chosen** billing
path and is kept; every *other* metered redirect (`ANTHROPIC_AUTH_TOKEN`,
Bedrock, Vertex) is still scrubbed so a run only ever bills one path. In both
modes the key lives only in this `.env` file, never in `config.yaml`.

```yaml
server:
  host: 127.0.0.1
  port: 8420                      # nh dashboard / API bind

llm:
  auth_mode: subscription         # subscription (default) | api_key — see auth modes below
  primary_model: claude-sonnet-5  # implementer (coder)
  review_model: claude-opus-5   # fresh-context reviewer + eval judge (different model)

database:
  path: ~/.no_human/no_human.db   # SQLite (WAL). No Postgres/Redis.

notifications:                    # write-only notify-out; null = log only
  slack_webhook_url: null         # Slack incoming webhook
  teams_webhook_url: null         # Microsoft Teams — a Power Automate WORKFLOWS
                                  # webhook. NOT a classic Office 365 connector:
                                  # those were disabled in May 2026 and a
                                  # connector URL is refused, not posted to.
  board_url: null                 # optional deep link; becomes the Teams card's
                                  # "Open in no_human" button. Leave null unless
                                  # the board is reachable from where Teams is
                                  # read — a 127.0.0.1 link is dead on a phone.
  email_to: you@example.com

integrations:
  jira:                           # polled intake (not `nh task add`); off by default
    enabled: false
    site: ""                      # https://you.atlassian.net
    project_key: ""
    email: ""                     # paired with JIRA_API_TOKEN as Basic auth
    jql: ""                       # operator-authored; blank = open issues in project
    default_repo: ""              # where polled-in tasks run
    write_back: false             # opt-in: comment + category-matched transition
    poll_interval: 5m             # floor 60s
  linear:                         # polled intake; off by default
    enabled: false
    team_key: ""                  # e.g. "ENG" — the prefix in ENG-123
    state_types: [triage, backlog, unstarted]   # which workflow states to pull in
    label: ""                     # optional: only issues carrying this label
    default_repo: ""
    write_back: false             # opt-in: comment + type-matched state move
    poll_interval: 5m             # floor 60s

approval:
  require_before_merge: true      # ALWAYS true — agent never merges
  auto_merge_on_approval: false   # there is no auto-merge
  approval_timeout: 24h           # re-notify; never auto-proceed

git:
  branch_prefix: "no-human/"
  commit_prefix: ""
  never_push_to: ["main", "master", "release/*"]
  agent_identity_name: "no_human"
  agent_identity_email: "no-human@acme.com"   # distinct from you

safety:
  max_files_changed: null         # no size cap by default; set an int to escalate
  max_lines_changed: null         # SCOPE_EXPLOSION past it. The human is the gate.
  forbidden_paths: [".env", "secrets/", "*.key", "*.pem"]
  block_test_weakening: true

bounds:
  max_attempts: 3
  max_turns_per_attempt: 500
  lifetime_attempts: 9             # across resumes; exhausting it parks BUDGET_EXHAUSTED
  max_correction_rounds: 2         # also caps autonomous PR-comment->revise rounds;
                                   # exceeding it escalates to a human (no infinite revise)

hooks:
  per_edit_lint: true             # B1: after each Edit/Write, lint the changed file and
                                  # feed hard errors straight back to the agent. A no-op
                                  # unless the repo has a confirmed lint command.

blockers:                         # Part 22
  max_alternatives_before_escalate: 2
  max_park_duration: "48h"        # parked past this => escalate (never abandon)
  wake_poll_interval: "10m"
  transient_infra_retries: 2
  escalate_on_low_confidence_below: 0.6   # unsure what's wrong => ask, don't thrash

ci:                               # opt-in; the install-wide fallback (see below)
  enabled: false
  # One of: gitlab | github_actions | jenkins | circleci | ghe_checkruns.
  # Each reads a DIFFERENT required key — see docs/adapters.md#ci-no_humanci for the
  # per-backend table. A key another backend needs is ignored, not rejected.
  backend: gitlab
  project: ""                     # gitlab: "group/subgroup/repo"
                                  # circleci: the slug "<vcs>/<org>/<repo>",
                                  #   e.g. "gh/acme/svc" — NOT a path
  # repo: "org/repo"              # github_actions / ghe_checkruns
  # workflow: "ci.yml"            # github_actions
  # job: "job/folder/job/main"    # jenkins
  # base_url: https://build.example.com   # jenkins — required, the default is
                                  #   a placeholder and will not resolve
  # auth: token                   # jenkins: token (basic) | cookie (SSO)
  hostname: gitlab.acme.net
  mode: watch                     # watch = poll the pipeline your push started
                                  # trigger = start one (jenkins/circleci opt-in)
  variables: {}                   # extra pipeline variables (POST body array)
  timeout_minutes: 60
  max_infra_retries: 2            # retry infra failures after 120s, max 2
  poll_interval: 30
  result_parser: pytest           # or "surefire" for Maven

  # NOTE: if `enabled` is true but the chosen backend cannot be built — a
  # missing required key above, or a misspelled `backend` — the run proceeds
  # with the LOCAL test suite as its only gate. It is no longer silent about
  # that (see "which source wins" below), but it does not stop either. Whether
  # it SHOULD stop is open: docs/KNOWN_ISSUES.md KI-5.

ci_gate:                          # post-PR CI gate (WakeWatcher rung 5)
  enabled: false                  # `nh ci-gate run <task>` force-enables for one run
  project_id: 12345               # your CI project's numeric id
  hostname: gitlab.example.com
  ref: main
  repos: [<your-service>]         # PR repos this gate governs
  namespace_template: "ci-gate-pr{pr_number}"   # throwaway, collision-guarded
  namespace_variable: CI_GATE_NAMESPACE         # pipeline var carrying the namespace
  variables: {}                   # extra pipeline variables
  poll_interval: 30
  timeout: 3600
  kubeconfig: ~/.kube/configs/<your-ci-cluster>.yaml   # latest_dev images + ns guard
  pr_build: true                  # code PRs: build the image FROM the PR via the
                                  # Jenkins enrich job (external SSO trigger); false
                                  # = escalate code PRs honestly instead
  enrich_job_url: https://build.example.com/<controller>/job/<folder>/.../<image-build-job>
  jenkins_controller: https://build.example.com/<controller>
  registry_prefix: registry.example.com/<org>/<image-path>
```

### `ci:` — which source wins

A run's CI backend is resolved from three places, **most specific first**:

1. an explicit backend injected by an embedder (rare; tests use this),
2. the **project profile's** `ci` block, written by `nh onboard` and confirmed
   by you — it describes *this* repo,
3. the global **`ci:`** block above — the install-wide fallback.

The profile wins over the global block because it is the more specific
statement: `~/.no_human/config.yaml` describes every repo this install will
ever touch, so setting both can only mean "this one is different". A profile
block that names no pipeline target (`project` / `repo` / `job`) is treated as
a detection hint rather than a claim — `nh onboard` writes a bare
`{backend: gitlab}` just for seeing a `.gitlab-ci.yml` — so it falls through to
the global block instead of overriding it.

If a source asks for CI but cannot produce a backend (say `enabled: true` with
an empty `project`), the run does **not** silently proceed ungated: it emits an
`advisory` event naming the source and the reason, and `nh doctor` reports
`CI BACKEND UNUSABLE`. A gate you believe in but do not have is worse than no
gate, so this case is always visible.

### `llm.auth_mode` — two modes

**`auth_mode: subscription` (default).** The coding backend runs on
`CLAUDE_CODE_OAUTH_TOKEN`, loaded from `~/.no_human/.env` with a process-env
fallback. All other metered vars are scrubbed from the process on startup, and
a stray `ANTHROPIC_API_KEY` aborts the run rather than silently billing the
metered API. `auth_profile` (`nh auth use <profile>`) selects which
subscription — personal or enterprise — pays; a run never spans two profiles.

**`auth_mode: api_key` (operator-chosen BYO-API-key).** The one sanctioned
exception, for friends/commercial installs that pay Anthropic directly with
**their own** `ANTHROPIC_API_KEY`. Specifics:

1. The key lives **only** in `~/.no_human/.env` (`chmod 600`) — it is never
   written to or read from `config.yaml`; only the `auth_mode` string itself
   is a config value.
2. This is the operator's **chosen** billing path: the run pays Anthropic
   directly through the operator's own metered key, not the shared
   subscription.
3. Every **other** metered redirect — `ANTHROPIC_AUTH_TOKEN`, Bedrock
   (`AWS_BEARER_TOKEN_BEDROCK`), Vertex (`GOOGLE_APPLICATION_CREDENTIALS`) — is
   still scrubbed from the process, so a run bills exactly one path.
4. No OAuth token is exported into the process env in this mode.
5. The run is attributed to the `api_key` profile for cost/audit tracking.

## Tests command

`tests.command` (optional) overrides test detection for the local suite the
orchestrator runs after review. If unset, a sensible default is detected
(`pytest`, etc.). Held-out tests go in `tests/held_out/` and are run separately.

## Per-task config snapshot

Each task stores the `config` it ran under (`tasks.config`), so a task's
behaviour is reproducible even if the global config later changes.

## `.no_human.yml` — config the repo carries itself (C3-G2)

A target repo can ship its own hints so no_human works well on it without the
operator re-teaching every install. The file is **untrusted input** (whoever
wrote the repo wrote it), so the contract is narrow: **it may only ADD hints or
TIGHTEN safety.**

```yaml
# <repo>/.no_human.yml
test_commands:                       # change-scoped routing, not the gate itself
  - glob: "web/**"
    command: "node --test src/"
    cwd: "web"                       # must stay inside the repo
playbook_hints:                      # advisory lines shown to the coder
  - "run `make check` before pushing; CI runs it too"
forbidden_paths_extra:               # append-only: adds to safety.forbidden_paths
  - "infra/**"
```

Exactly those three keys are read; **everything else is ignored** — a repo can
never set `test_cmd`, `never_push_to`, models, or auth. Further limits:

- The **operator's onboarded profile always wins**: repo routing rules apply only
  where `nh onboard` left none. The default test command stays operator-owned and
  proven, and routing only applies at all once the repo has a usable profile.
- A rule whose glob matches **everything** (`**`, `*`, …) is rejected — that is a
  gate override, not change-scoped routing.
- The file is **snapshotted once per run**, before the coder session starts, so an
  agent cannot rewrite the gate it is judged by mid-attempt.
- Malformed, oversized (>16KB), or absent ⇒ ignored entirely; it never fails a run.
- Hints are advisory: they never outrank the acceptance criteria, and they cannot
  cross a safety rail (the guard, not the prompt, enforces those).

Applied config is visible in the task timeline as a `repo_config` event.
