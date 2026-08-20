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
| `llm.review_model` | `claude-opus-4-8` | The fresh-context reviewer |
| `llm.review_timeout_seconds` | `1500` | Wall-clock seconds one reviewer session gets before it is cut off. Raise it if reviews time out; a review round measured ~1078s (worst 1357s) on the Opus reviewer tier |
| `llm.code_review_timeout_seconds` | `1800` | The same wall for `code_review` mode, which reads a whole PR diff at twice the gate's cap |
| `bounds.max_attempts` | `3` | Implement/review cycles in one loop |
| `bounds.max_turns_per_attempt` | `500` | Agent turns before an attempt is cut off |
| `server.port` | `8420` | Web board bind port |
| `concurrency.enabled` | `false` | Parallel task workers, each in its own worktree |
| `worker.backend` | `claude` | Which coding backend the IMPLEMENTER runs on: `claude` (the Claude Agent SDK) or `codex` (the OpenAI Codex CLI, on your own `OPENAI_API_KEY`). Only the coder moves — reviewer, planner, supervisor and utility stay on Claude. See [BACKENDS.md](BACKENDS.md) |
| `ci.enabled` | `false` | Trigger and poll GitLab CI, GitHub Actions, Jenkins or CircleCI |
| `pipeline.review_routing.enabled` | `true` | Review depth scales with diff size — see below |
| `pipeline.review_routing.max_diff_lines` | `200` | The single-turn-gate threshold, in added+deleted lines |
| `usage_ledger.retention_days` | `90` | Age past which `unattributed_usage` rows are rolled up (not deleted — totals stay exact, per-row `ts`/`task_id` detail is lost); `0` disables compaction |

Review depth scales with diff size: a gate review of a diff at or under
`max_diff_lines` changed lines runs SINGLE-TURN, no tools — the diff, the full
text of every changed file, lint and wiring evidence are already in the
prompt, so the exploration turns buy nothing. Any diff containing a
risk-flagged pattern ALWAYS gets the full multi-round review regardless of
size: a guard/scrub function touched (detected by path AND by diff content,
so a guard function in an otherwise generic file — e.g. `install_pre_push_guard`
in `vcs/push_hook.py` — is still caught), a test file deleted or renamed away,
or a security-sensitive path (`auth`, `crypt`, `secret`, `credential`, `token`,
`key`, `.env`, `config.yaml`, `config.py`, `.github/workflows/**`,
`.githooks/**`) — as does a diff too big to measure (binary) or a re-review
after a prior round already failed. `enabled: false` restores the
pre-2026-08-14 behaviour (every gate review is full). See
`src/no_human/core/review_routing.py`.

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
| `MONDAY_API_TOKEN` | monday.com intake (`integrations.monday.enabled`). Create at monday.com → Administration → Connections → API. Sent raw as `Authorization`, not `Bearer`. See [adapters.md](adapters.md#mondaycom-specifics). |
| `JENKINS_USER`, `JENKINS_API_TOKEN` | Repos whose CI is Jenkins (`build.example.com`) or human-gated on a `Jenkinsfile`. Basic auth — the default `ci.auth: token` mode. |
| `SSO_USERNAME`, `SSO_PASSWORD` | Jenkins controllers that reject API-token basic auth, i.e. `ci.auth: cookie`. Used once to capture a session cookie. |
| `CIRCLECI_TOKEN` | `ci.backend: circleci`. A CircleCI personal API token; sent as the `Circle-Token` header. |
| `GITLAB_TOKEN` | Repos whose CI backend is GitLab, or whose VCS host is a GitLab. |
| `GH_ENTERPRISE_TOKEN` | Opening PRs against a GitHub-Enterprise host (e.g. `code.example.com`). Public `github.com` uses `gh auth login` instead. |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | Only for the opt-in Slack Socket-Mode worker (`integrations.slack.intake`). **The worker connects but does not yet create tasks from mentions — the intake handler is not wired in `nh serve`.** Unrelated to notify-out. |

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
  review_model: claude-opus-4-8   # fresh-context reviewer + eval judge (different model)
  review_timeout_seconds: 1500    # wall-clock per review session; a round that
                                  # dies on this wall escalates UNREVIEWED
  code_review_timeout_seconds: 1800  # same, for `nh review` on a whole PR diff
  local_model: null               # worker.backend: local — model id the local server serves
  local_base_url: null            # e.g. http://localhost:8000 — required in local mode
  local_cli_path: null            # null ⇒ the SDK-bundled CLI

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
  monday:                         # polled intake; off by default
    # monday has NO typed workflow state — a status column is user-defined
    # labels that differ per board — so the label→meaning mapping is stated
    # here explicitly and is never inferred. With board_id/status_column unset
    # the adapter RAISES rather than silently returning nothing.
    enabled: false
    board_id: ""                  # numeric board id, as a string
    status_column: ""             # the column's ID (e.g. bug_status), NOT its title
    todo_labels: []               # labels meaning "not started", e.g. ["Ready for Dev"]
    in_progress_label: ""         # optional: label to move to when work starts
    done_label: ""                # optional: label to move to on completion
    default_repo: ""
    write_back: false             # opt-in: update (comment) + status-label move
    poll_interval: 5m             # floor 60s
  # No circleci block: like github_actions / gitlab / jenkins, CircleCI is
  # configured in the `ci:` block — set backend: circleci and project to the
  # API v2 project slug "<vcs>/<org>/<repo>" (e.g. gh/your-org/your-repo), with
  # CIRCLECI_TOKEN in ~/.no_human/.env. It used to live here as
  # org_slug + project + enabled, and nothing read any of the three.
  slack:
    intake: false                 # opt-in Socket-Mode worker; needs SLACK_BOT_TOKEN
                                  # + SLACK_APP_TOKEN in .env. NOTE: connects only —
                                  # mention-to-task intake is not yet wired in serve
  teams:
    enabled: true                 # mute switch over the notify-OUT channel.
                                  # The webhook URL itself is NOT here — it
                                  # stays at notifications.teams_webhook_url,
                                  # where the notifier reads it. Set this false
                                  # to silence Teams without deleting the URL.

The first-run wizard's **Connect your tools** step edits everything in this
`integrations:` block, and Settings → Integrations edits it afterwards. Neither
takes a credential: every token stays in `~/.no_human/.env`, and the wizard
names the variable rather than accepting a value — `config.yaml` is
world-readable. `enabled` (and Slack's `intake`) is what actually starts a
poller or a worker (for Slack: the connection only — mention intake is not yet
wired), so an integration with every setting filled in but
`enabled: false` does nothing, and both UIs say so rather than reporting it as
configured.

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
  approve_identity:               # who a human merge is attributed to
    name: ""                      # empty -> resolved from this repo's git
    email: ""                     # config (user.name/user.email)

`approve_merge.enabled` (default **true**) is what makes `nh approve` LAND the
pull request: squash the branch into one commit, push it to the default branch,
and close the PR. Set it to `false` and `nh approve` still records your
approval and still marks the task approved — it just does not merge, leaving
the PR for you to merge in your git host. The same record-only path is taken
when there is no PR or no `gh` on PATH; none of those is a failure.
`approve_merge.test_timeout_seconds` (default **1800**) bounds the test run
that gates that landing.

`git.approve_identity.name`/`.email` is the identity the ONE commit `nh
approve` lands when it squash-merges a PR is attributed to — the human merge
action (constraint #2), never the agent's. Left empty (the shipped default),
it resolves to git's own `user.name`/`user.email` for that repo (repo-local
config overriding global), the same identity a plain `git commit` there
would use; it is deliberately never `git.agent_identity_name`/`_email`. Set
both fields to override per install. If neither the config nor git yields
both `name` and `email`, `nh approve` refuses with an explicit message
rather than guessing.

safety:
  max_files_changed: null         # no size cap by default; set an int to escalate
  max_lines_changed: null         # SCOPE_EXPLOSION past it. The human is the gate.
  forbidden_paths: [".env", "secrets/", "*.key", "*.pem"]

bounds:
  max_attempts: 3
  max_turns_per_attempt: 500
  lifetime_attempts: 9             # across resumes; exhausting it ends the task (see budget:)
  max_correction_rounds: 2         # also caps autonomous PR-comment->revise rounds;
                                   # exceeding it escalates to a human (no infinite revise)

budget:
  exhaustion_terminal: true       # an exhausted lifetime budget ENDS the task (status
                                  # failed) with its full BUDGET_EXHAUSTED record and a
                                  # wake condition naming what would revive it - it does
                                  # not ask "spend more, or stop here?". The answer to
                                  # that question was standing policy ("stop; the ticket
                                  # was too big - refile it smaller"), and asking it was
                                  # 69 of 119 human-blocking questions. Set false to be
                                  # asked. Raising a cap is human-only either way:
                                  # `nh task config <id> lifetime_tokens=N`.

hooks:
  per_edit_lint: true             # B1: after each Edit/Write, lint the changed file and
                                  # feed hard errors straight back to the agent. A no-op
                                  # unless the repo has a confirmed lint command.

blockers:                         # Part 22
  max_park_duration: "48h"        # parked past this => escalate (never abandon)
  wake_poll_interval: "10m"
  escalate_on_low_confidence_below: 0.6   # unsure what's wrong => ask, don't thrash
  challenge: true                 # ONE supervisor-checked challenge per task, for the
                                  # judgment-call blocker categories only (AMBIGUITY,
                                  # NOVEL_UNKNOWN, IMPOSSIBLE). A "resolvable" verdict
                                  # costs that attempt and re-enters the bounded loop
                                  # under a recorded reversible assumption; every
                                  # external category, every second blocker and every
                                  # check failure park exactly as before. Set false to
                                  # park on the first blocker, unchallenged.

usage_ledger:
  retention_days: 90              # unattributed_usage rows older than this are rolled
                                   # up (not deleted) into one row per (site, model);
                                   # 0 disables compaction

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

### `llm.local_model` / `llm.local_base_url` / `llm.local_cli_path`

Only read when `worker.backend: local`. `local_base_url` is **required** in
that mode — an ambient `ANTHROPIC_BASE_URL` is scrubbed and never trusted as a
fallback. It must be `http` or `https`, and the host must be `localhost` or a
**literal** loopback/RFC1918 IP address: a DNS name is refused (it is
re-resolved at connect time, which is a rebinding surface) and a
public/routable IP is refused (local mode must not leave the machine). Port
numbers and paths are not validated — `http://localhost:8000` and
`http://127.0.0.1:1234/v1` are both fine. The URL must not embed userinfo
(`http://user:pass@host`) or a key-looking query parameter — the mode lives in
config, the key never does. If the local server enforces a key, it goes in
`~/.no_human/.env` as `LOCAL_LLM_API_KEY`, never in `config.yaml`.
`local_cli_path` is optional; `null` uses the SDK-bundled CLI.

## `learning:` — memory lifecycle

Memory lifecycle C (`docs/design/memory-lifecycle-triage.md`) — retirement and
flood control for rules/skills. Defaults, pinned to `config.DEFAULT_CONFIG` the
same way the table above is:

| Setting | Default | What it does |
|---|---|---|
| `learning.archive_unconfirmed_days` | `45` | Unconfirmed (`confirmed = 0`), `source="proposed"` rows older than this are auto-archived by the daily `RetirementSweepJob` — reversible, never deleted |
| `learning.retire_suggest_days` | `90` | Confirmed rules unused this long surface in the `retire?` SUGGEST-only section — never auto-archived |
| `learning.sweep_interval_seconds` | `86400` | How often the retirement sweep ticks; the first tick runs immediately at boot |
| `learning.sweep_enabled` | `true` | Kill switch for the unattended daily sweep (the CLI triage path stays reachable either way) |
| `learning.propose_on_success` | `false` | The flood-kill: the per-success templated skill proposal only fires when this is explicitly turned on |

Three mechanisms, concretely:

1. **45-day auto-archive.** `Store.archive_unconfirmed_older_than` sweeps
   unconfirmed, `source="proposed"` rows past `archive_unconfirmed_days`
   (default 45) once a day and once at boot. Reversible — it sets
   `archived = 1`, never `DELETE`s.
2. **Flood-kill.** The per-success templated proposal (`learning/queue.py`'s
   `_build`) is gated behind `propose_on_success`, **off** by default — the
   flood source that historically produced ~394 near-duplicate pending rows
   is simply not invoked unless an operator opts in. `nh learnings
   --triage-templated [--apply]` cleans up any pre-existing backlog from
   before this gate existed.
3. **Supersede-on-confirm.** `Store.supersede_memory`, called from
   `LearningQueue.confirm`, archives the oldest matching *active* row with
   `superseded_by` pointing at the newly confirmed survivor when a confirmed
   near-duplicate exists — never more than one hop, never a chain.

The Rules/Skills panel surfaces all three: an **Archived**/**Superseded**
badge, a "Show archived" filter, and **Restore**/**Dismiss** triage actions —
see `docs/design/memory-lifecycle-triage.md`'s "Rules/Skills UI" section for
the exact contract.

## Tests command

`tests.command` (optional) overrides test detection for the local suite the
orchestrator runs after review. If unset, a sensible default is detected
(`pytest`, etc.). Held-out tests go in `tests/held_out/` and are run separately.

## Lint command

`lint.command` (optional) overrides lint detection the same way. It decides
whether the lint gate exists at all: with no explicit command and no proven
`lint_cmd` on the repo profile, the gate is SKIPPED — no lint, no gate — which
is deliberate, because linting a repo with a command nobody confirmed produces
noise the agent then "fixes". Neither this key nor `tests.command` appears in
the defaults file; both are read straight from your config, so setting either
one is how you turn the behaviour on.

## Intake grill

`intake.grill` (default **true**) decides whether the clarifying-questions
stage runs before planning. It is the most expensive pre-plan stage — two LLM
sessions on every task — so setting it `false` is the way to turn that cost
off; a small prose-only change skips it automatically regardless. Like
`lint.command` and `tests.command`, the `intake` section is not written into
the defaults file: set it yourself to change the behaviour.

## Timeouts read straight from your config

Two wall-clock ceilings are read the same way and default generously so a
legitimately long run is never cut off:

- `bounds.attempt_timeout_s` (default 3600) — one coder attempt. It was the
  single unbounded call before it existed.
- `bounds.shadow_timeout_s` (default 1800) — one shadow/bench run in the
  throwaway sandbox.

## Keys the doc gate cannot see

`tests/test_config.py` sweeps the source for settable keys, but it only sees a
two-level chain read directly off the config (`config.get("a", {}).get("b")`).
A section pulled into a local variable first, and a single top-level key, are
both invisible to it — so these are documented by hand. If you add a key of
either shape, add it here too: nothing will remind you.

- `reanalysis.enabled` (default **true**), `reanalysis.interval_seconds`
  (86400, floored at 60), `reanalysis.days` (30), `reanalysis.max_proposals`
  (20) — the periodic job that mines **IDE conversation transcripts** from the
  last N days and proposes learnings from them. It is not reading this
  product's task history: it asks the running IDE language servers for their
  transcripts, and with no IDE running it finds nothing and proposes nothing.
  `max_proposals` does not cap anything — the proposals are already committed
  when the count is checked, so exceeding it logs a warning and leaves them
  for you to triage. Turning `enabled` off stops this unattended pass;
  `nh history --analyze` ignores the flag and still works. (`nh serve` starts
  the job, so it does honour it.) The `reanalysis` section is not written into
  the defaults file.
- `onboarding.extra_scan_roots` — extra directories the repo-discovery scan
  looks in, beyond the conventional clone roots. A single string is accepted
  as well as a list, and a leading `~` means the home the scan is bound to.
  **It cannot reach outside your home directory**: a root that resolves
  elsewhere is refused, by design. For repos on another volume use the
  onboarding UI's "Search another folder", which takes any path.
- `max_thinking_tokens` (default 10000) — a TOP-LEVEL key, not nested under
  `llm`. It caps extended thinking on models that support it, and applies only
  when the task's computed complexity tier turns thinking on; there is no way
  to request it per task.

`ci.workflow` and `ci.repo` are two more keys of this shape. Both are already
shown in the `ci:` block above, as commented-out lines the gate's matcher
cannot parse (it skips `#`-led lines, by design — a commented example is not a
declaration).

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
