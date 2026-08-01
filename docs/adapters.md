# Adapters

no_human is a lean single-backend system (one Claude Agent SDK backend, SQLite,
agentic grep — no RAG/pgvector). "Adapters" are the read/write integrations
around that core. They degrade gracefully: a missing CLI or down source is a
best-effort skip, not a crash.

## Intake (`no_human/intake/`)

Intake has **two distinct shapes**, and they are not interchangeable.

**1. `nh task add` sources.** A URL or id you pass on the command line is
detected by `parse_source` and routed to an adapter that produces a `Task`
(title, description, acceptance criteria, external id).

| Source | How | Notes |
|--------|-----|-------|
| **GitHub Issues** | `gh api` | includes GitHub Enterprise hosts |
| **GitLab Issues** | `glab api` | includes self-hosted GitLab |
| **Freeform** | `--title` / `--description` / `--criteria` | no external system |

```bash
nh task add https://github.com/org/repo/issues/12 --repo /path/to/repo
nh task add --title "Add greet(name)" --repo /path/to/repo --criteria "returns 'hi, X'"
```

**2. Polled trackers.** Jira and Linear are *not* `nh task add` arguments.
They are server-side pollers that `nh serve` / `nh start` tick on their own
cadence, creating one task per new issue matching an **operator-authored**
filter. Both are opt-in and off by default, both dedupe on
`(source, external_id)`, and both have opt-in write-back.

| Tracker | Module | API | Credential | Filter |
|---|---|---|---|---|
| **Jira Cloud** | `intake/jira.py` + `jira_poll.py` | REST `/rest/api/3/search/jql`, HTTP Basic `email:token` | `JIRA_API_TOKEN` | `integrations.jira.jql` |
| **Linear** | `intake/linear.py` + `linear_poll.py` | GraphQL `https://api.linear.app/graphql` | `LINEAR_API_KEY` | `integrations.linear.team_key` + `state_types` + `label` |

A tracker's filter is never built from a task's own text. A transport error
logs and is retried on the next tick; it never crashes the pool and never
half-creates a task.

### Linear specifics

Three API facts the adapter is built around, because getting any of them wrong
produces a silent or misdiagnosed failure:

- **The auth header is the raw key** — `Authorization: <key>`, *not*
  `Bearer <key>`. `Bearer` is for OAuth access tokens only. The wrong form is a
  401 that looks like a bad key.
- **Rate limiting is HTTP 400, never 429**, with `extensions.code ==
  "RATELIMITED"`. Branching on 429 never fires; treating 400 as permanent gives
  up on a retryable failure. The poller names throttling explicitly in its log.
- **Status alone does not classify a failure.** Field errors arrive at 200,
  auth failure at 401, throttling at 400 — all three carry an `errors` array,
  so it is parsed on every response.

`WorkflowState.type` is a plain `String` (not an enum) with seven values:
`triage`, `backlog`, `unstarted`, `started`, `completed`, `canceled`,
`duplicate`. Write-back resolves the team's **own** state of the target type at
runtime — never a hardcoded state UUID, since those are per-team — and picks
the lowest-`position` state of that type, which is the order Linear itself
displays them in. `canceled` and `duplicate` are refused outright: closing an
issue is a human action (constraint #2).

Polling, not webhooks: Linear does offer webhooks with HMAC-SHA256
`Linear-Signature` verification, but they need a publicly reachable HTTPS
endpoint, and no_human binds to `127.0.0.1`. Polling costs ~60 requests/hour at
the 60s floor against a 2,500/hour personal-key budget.

## Context (`no_human/context/`)

Read-only gatherers run in parallel with a per-source timeout; one slow/bad
source can't abort the rest. Completeness is a **binary** named-artifact check,
never a score.

- **codebase** — agentic grep/glob + `git log`, vendored dirs excluded.
- **sessions** — past no_human session memory.
- **Teams / Outlook** — read-only Microsoft Graph `/search/query`
  (`context/teams.py`, `context/outlook.py`), driven by a **read-only** token
  in `context.m365.token`.

```bash
nh task context <id>   # gather + show, no implementation run
```

> **Teams appears twice in this document, in two unrelated roles.**
> `context/teams.py` **reads** messages over the Graph API for background.
> `notify/teams.py` **writes** an alert into a channel over a webhook. They
> share no credential, no direction and no failure mode. Configuring one does
> nothing for the other.

## Notifications (`no_human/notify/`)

Write-only "a human is needed" channels. Both are optional and off by default;
with neither configured, notifications are logged. `notify.build_notifier`
fans out to every configured channel, and reports success **only if every
enabled channel accepted the message** — a partial delivery is a failure, and
the failing channel is named in the log.

| Channel | Config key | Payload |
|---|---|---|
| **Slack** | `notifications.slack_webhook_url` | `{"text": …}` to an incoming webhook |
| **Microsoft Teams** | `notifications.teams_webhook_url` | Adaptive Card to a Power Automate Workflows webhook |

### Teams: use a Power Automate Workflows webhook, not a connector

Office 365 / Microsoft 365 connectors — the classic Teams *Incoming Webhook*
with an `outlook.office.com/webhook/…` or `<tenant>.webhook.office.com/…` URL —
were **disabled by Microsoft between 2026-05-18 and 2026-05-22 and no longer
function**. `notify/teams.py` detects those URLs by host and **refuses to post**,
logging what to do instead, rather than firing at a dead endpoint. The
integrations panel reports such a URL as configured-but-broken, not as a
working channel.

Create the replacement in Teams: **Workflows app → "Post to a channel when a
webhook request is received" → pick team + channel → copy the URL.** No Entra
app registration, no client secret, no OAuth flow. Notes:

- The URL carries its own SAS credential in the query string — **treat the
  whole URL as a secret**. It is never logged, and `/api/config` scrubs it.
- The payload is an **Adaptive Card**, pinned to schema **1.2**. Teams desktop
  supports up to 1.5, but the Teams *mobile* app supports only up to 1.2 and
  later versions may not render — an alert gets read on a phone. The legacy
  MessageCard format is deliberately not used: it still posts, but Microsoft
  documents that its buttons do not render, which is a silent half-delivery.
- Success is **any 2xx** (the stock template answers `202`, not `200`).
- No host is allowlisted. Only the known-dead connector hosts are rejected, so
  a future flow endpoint on a new host keeps working.
- Microsoft Graph is not usable here: `POST /teams/{id}/channels/{id}/messages`
  has **no application permission** for ordinary sends (only
  `Teamwork.Migrate.All`, which is migration-only), so an unattended tool would
  need a delegated user token.
- A Power Automate flow that is not triggered for 90 days **may be turned
  off**. That is why a failed post logs and returns false instead of being
  swallowed — a channel that quietly stopped delivering must be visible.

## VCS (`no_human/vcs/`)

Deterministic, orchestrator-owned (never the LLM). Open-PR backend is selected
by remote:

- **GitHub** via `gh pr create`
- **GitLab** via `glab mr create`
- **local bare repo** fallback (used in tests / offline)

Guards: `never_push_to`, protected-branch refusal, `git merge` / force-push
blocked by the PreToolUse hook.

## CI (`no_human/ci/`)

Opt-in per project (`ci.enabled`). GitLab backend:

- **trigger** `glab api --hostname {host} --method POST projects/{enc}/pipeline
  --input body.json` with body `{"ref": {b}, "variables": [{"key","value"}…]}`
  (`glab ci run` is broken on gitlab.acme.net: defaults to gitlab.com →
  401, drops variables)
- **poll** `glab api projects/{enc}/pipelines/{id}` + `.../jobs`
- **infra vs real** failure discrimination → infra auto-retries (120 s, max 2),
  real failures loop back to implement within `max_attempts`.
- **result parsers**: `pytest` summary, Maven `surefire` (`Tests run: X, …`).

Add a backend by implementing the `ci/base.py` contract and wiring it in
`ci/__init__.py:ci_from_config`.
