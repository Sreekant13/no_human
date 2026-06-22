# Configuration

Config lives at `~/.no_human/config.yaml`, auto-generated with defaults on first
run. The user's values are deep-merged over the defaults. The metered
`ANTHROPIC_API_KEY` must never appear here (loading rejects it).

Secrets live separately in `~/.no_human/.env` (`chmod 600`, gitignored):
only `CLAUDE_CODE_OAUTH_TOKEN` (and optionally `SLACK_WEBHOOK_URL`).

```yaml
server:
  host: 127.0.0.1
  port: 8420                      # nh dashboard / API bind

llm:
  auth_mode: subscription         # subscription only — never metered API
  primary_model: claude-opus-4-8  # implementer
  review_model: claude-sonnet-4-6 # fresh-context reviewer + eval judge (different model)

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
  auto_pr: true

safety:
  max_files_changed: 20           # exceed => SCOPE_EXPLOSION escalation
  max_lines_changed: 500
  forbidden_paths: [".env", "secrets/", "*.key", "*.pem"]
  block_test_weakening: true

bounds:
  max_attempts: 3
  max_turns_per_attempt: 40
  escalate_after: 3
  max_correction_rounds: 2

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
  variables: {}                   # extra KEY:VALUE for `glab ci run`
  timeout_minutes: 60
  max_infra_retries: 2            # retry infra failures after 120s, max 2
  poll_interval: 30
  result_parser: pytest           # or "surefire" for Maven
```

## Tests command

`tests.command` (optional) overrides test detection for the local suite the
orchestrator runs after review. If unset, a sensible default is detected
(`pytest`, etc.). Held-out tests go in `tests/held_out/` and are run separately.

## Per-task config snapshot

Each task stores the `config` it ran under (`tasks.config`), so a task's
behaviour is reproducible even if the global config later changes.
