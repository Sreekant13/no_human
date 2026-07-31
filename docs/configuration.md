# Configuration

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
| `SLACK_WEBHOOK_URL` | Optional — write-only notify-out channel (else logs only). |
| `JENKINS_USER`, `JENKINS_API_TOKEN` | Repos whose CI is Jenkins (`build.example.com`) or human-gated on a `Jenkinsfile`. |
| `GITLAB_TOKEN` | Repos whose CI backend is GitLab, or whose VCS host is a GitLab. |
| `GH_ENTERPRISE_TOKEN` | Opening PRs against a GitHub-Enterprise host (e.g. `code.example.com`). Public `github.com` uses `gh auth login` instead. |
| `TRACKER_TOKEN` / TRACKER creds | TRACKER/Acme intake + traceability writes (see `docs/adapters.md`). |

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

notifications:
  slack_webhook_url: null         # write-only alert webhook; null = log only
  email_to: you@example.com

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
  max_files_changed: 20           # exceed => SCOPE_EXPLOSION escalation
  max_lines_changed: 500
  forbidden_paths: [".env", "secrets/", "*.key", "*.pem"]
  block_test_weakening: true

bounds:
  max_attempts: 3
  max_turns_per_attempt: 60
  escalate_after: 3
  max_correction_rounds: 2         # also caps autonomous PR-comment->revise rounds;
                                   # exceeding it escalates to a human (no infinite revise)

hooks:
  per_edit_lint: false            # B1: after each Edit/Write, lint the changed file and
                                  # feed hard errors straight back to the agent. Default off
                                  # (no-op unless the repo has a confirmed lint command).

blockers:                         # Part 22
  max_alternatives_before_escalate: 2
  max_park_duration: "48h"        # parked past this => escalate (never abandon)
  wake_poll_interval: "10m"
  transient_infra_retries: 2
  escalate_on_low_confidence_below: 0.6   # unsure what's wrong => ask, don't thrash

ci:                               # opt-in per project
  enabled: false
  backend: gitlab
  project: ""                     # e.g. "group/subgroup/repo"
  hostname: gitlab.acme.net
  variables: {}                   # extra pipeline variables (POST body array)
  timeout_minutes: 60
  max_infra_retries: 2            # retry infra failures after 120s, max 2
  poll_interval: 30
  result_parser: pytest           # or "surefire" for Maven

integration-gate:                           # M6: post-PR integration gate (WakeWatcher rung 5)
  enabled: false                  # `nh integration-gate run <task>` force-enables for one run
  project_id: 12345               # your integration-gate project's numeric id
  hostname: gitlab.acme.net
  ref: main
  repos: [<your-service>]         # PR repos this gate governs
  namespace_template: "<your-prefix>-integration-gate-pr{pr_number}"  # throwaway, collision-guarded
  variables: {...}                # the proven static flag set (see config.py)
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
