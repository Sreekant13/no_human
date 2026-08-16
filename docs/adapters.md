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
| **Plain text** | positional SOURCE (a prose sentence) | no external system |

```bash
nh task add https://github.com/org/repo/issues/12 --repo /path/to/repo
nh task add "Add greet(name)" --repo /path/to/repo
nh task add --title "Add greet(name)" --repo /path/to/repo --criteria "returns 'hi, X'"
```

A positional source is either an **issue URL** — `parse_source` routes on
`/issues/` (GitHub) or `/-/issues/` (GitLab) — or a **plain sentence**, which
is filed directly using it as the task title (`is_plain_text_task`). **A
source-shaped token that is neither — including a bare ticket key like
`PROJ-42` or `owner/repo#12` — is not an intake source**: the CLI prints
`intake failed: not a recognized task URL/id` and exits 1. Use `--title` (or
just type the sentence positionally) to file that text as a freeform task.
The standalone tracker adapter that used to
accept bare keys has been removed; the trackers below are pollers, so a key
that exists in one arrives on its own rather than by being typed. The error
names what IS accepted, because typing the key is the first thing a developer
tries. Pinned by
`tests/test_intake.py::test_a_bare_tracker_key_is_rejected_not_ingested_as_freeform`.

**2. Polled trackers.** Jira, Linear and monday.com are *not* `nh task add`
arguments. They are server-side pollers that `nh serve` / `nh start` tick on
their own cadence, creating one task per new issue matching an
**operator-authored** filter. All are opt-in and off by default, all dedupe on
`(source, external_id)`, and all have opt-in write-back.

| Tracker | Module | API | Credential | Filter |
|---|---|---|---|---|
| **Jira Cloud** | `intake/jira.py` + `jira_poll.py` | REST `/rest/api/3/search/jql`, HTTP Basic `email:token` | `JIRA_API_TOKEN` | `integrations.jira.jql` |
| **Linear** | `intake/linear.py` + `linear_poll.py` | GraphQL `https://api.linear.app/graphql` | `LINEAR_API_KEY` | `integrations.linear.team_key` + `state_types` + `label` |
| **monday.com** | `intake/monday.py` + `monday_poll.py` | GraphQL `https://api.monday.com/v2` | `MONDAY_API_TOKEN` | `integrations.monday.board_id` + `status_column` + `todo_labels` |

A tracker's filter is never built from a task's own text. A transport error
logs and is retried on the next tick; it never crashes the pool and never
half-creates a task.

### Jira

- Descriptions arrive as **Atlassian Document Format** and are flattened to
  text; checklist lines become acceptance criteria.
- Search uses `/rest/api/3/search/jql` — the successor endpoint. The older
  `/rest/api/3/search` is on Atlassian's deprecation path.
- The token is `JIRA_API_TOKEN` in `~/.no_human/.env`, never in `config.yaml`
  and never logged. Auth is Atlassian Cloud HTTP Basic `email:token`, so
  `integrations.jira.email` must be the account the token belongs to. With
  `enabled: true` and no token the adapter reports itself unconfigured and the
  poller does nothing — it does not error.
- `write_back: true` posts work-note comments and advances the issue into its
  own workflow's In Progress / Done **status category**, resolved at runtime
  from the issue's available transitions — never a hardcoded transition id. It
  never closes an issue and never merges: those stay human actions.
- **A one-click hand-off from inside Jira** is possible without any of this
  changing, and the reason is worth stating because it constrains what such a
  hand-off can be. Anything running inside Jira — an Atlassian Forge app, an
  automation rule, a bulk edit by hand — **cannot** talk to no_human. It runs
  in Atlassian's cloud, and no_human runs on `127.0.0.1`. So it writes a
  **label**, and this poller picks the issue up on its next tick with a JQL
  like `labels = "no_human" AND statusCategory != Done`. The poller is the whole
  transport, so there is no inbound network path, no hosted tier, and nothing
  on the Jira side ever holds a credential of yours. Labelling the same issue
  twice cannot create two tasks — dedupe on `(source, external_id)` is what
  guarantees it, asserted in `tests/test_jira_label_roundtrip.py`.

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

**Config that matches nothing is refused, on the same terms as monday's.**
`team_key`, `state_types` and `label` are all server-side filters, and Linear
answers a filter that matches nothing with an empty page and no error — so an
unset team key, `N0` for `NO`, `Backlog` for `backlog`, or a label the
workspace does not have all look exactly like a team with no open work.
Measured against a real workspace: the correct config returned 4 issues and
each of those four faults returned 0, silently. Every one of them now raises
`LinearConfigError`, naming the setting:

- `state_types` is checked **offline** against the seven values above — they
  are a fixed documented list, so this costs no request and is also what the
  Settings panel and the onboarding wizard refuse a typo with;
- `team_key` and `label` are checked **against the workspace**, once per
  adapter, from inside the poll that was already making requests. Never at
  config load: no install should need Linear to be reachable to start.

The online half **fails open** — a validation query that errors, is throttled,
or comes back in an unrecognised shape leaves intake alone, because a check
that turned an API outage into "your config is wrong" would be worse than the
silence it replaces. A *correct* config that matches zero issues right now
still returns nothing and says nothing: what is validated is that the config
names real things, never that the result was non-empty. The poller reports a
config error on its own event kind (`linear_config_error`, the same split
monday's poller makes) and keeps ticking.

Polling, not webhooks: Linear does offer webhooks with HMAC-SHA256
`Linear-Signature` verification, but they need a publicly reachable HTTPS
endpoint, and no_human binds to `127.0.0.1`. Polling costs ~60 requests/hour at
the 60s floor against a 2,500/hour personal-key budget.

### monday.com specifics

**monday has no typed workflow state, and that is the whole design of this
adapter.** Jira exposes a status *category* and Linear a `WorkflowState.type`,
so "pull the backlog" means the same thing on any workspace. A monday status
column is a bag of user-defined labels — `Ready for Dev`, `Fixing`,
`Awaiting Review`, `Known Bug`, and often a blank one — that differs on every
board, and nothing in the API says which of them means "not started". Guessing
from label text, colour or `done_colors` would be wrong on the next board.

So the mapping is **explicit config**, and nothing is inferred:

```yaml
integrations:
  monday:
    board_id: "1234567890"
    status_column: bug_status        # the column's ID, not its title
    todo_labels: ["Ready for Dev"]   # what to pull
    in_progress_label: "Fixing"      # optional: where to move it on first claim
    done_label: "Fixed"              # optional: where to move it on completion
```

With `board_id` or `status_column` unset the adapter **raises** rather than
returning an empty list: an empty result is indistinguishable from an empty
board, so a typo'd install would look like a working one with no work in it.
The error names the exact config key and the query that discovers the value.

**Config that is wrong is refused on the same terms as config that is absent.**
Before it filters anything, every pull checks `status_column` and `todo_labels`
against the board's real columns and labels:

- a `status_column` that is not a column **id** raises, lists the ids that do
  exist, and — when the value is actually a column's *title* — says so and
  hands back the id to use, because that is the mistake people actually make;
- a `todo_labels` entry the board does not have raises, names the bad label and
  lists the board's real ones. **One bad label fails the whole pull**, not just
  its own share of it: continuing on the remaining labels would quietly narrow
  intake to a scope nobody chose, and work that stops arriving looks exactly
  like work nobody filed.

Both checks reuse the cached columns lookup — one request per adapter, never
one per item. A pull that stops at the 20-page bound is likewise reported as
**partial**, in the log and on the poller's event channel, rather than handed
back as if it were the whole board.

Discover the ids with the columns query — `status_column` takes the column's
**id** (`bug_status`), never its title (`Status`):

```graphql
{ boards(ids: [1234567890]) { columns { id title type settings_str } } }
```

For a `type == "status"` column, `json.loads(settings_str)["labels"]` is an
`{index: label}` dict — sparse indices, and one label is often the empty
string.

Three API facts the adapter is built around:

- **The auth header is the raw token** — `Authorization: <token>`, *not*
  `Bearer <token>`, the same as Linear. The wrong form is a 401 that looks like
  a bad token.
- **Rate limiting is HTTP 429 with an HTML body, not JSON.** This is the
  inverse of Linear's trap and it bites harder: there is no error code to
  branch on, so 429 is classified from the status *before* the body is parsed.
  A client that parses first reports its most common transient failure as
  "non-JSON response" and never retries it as throttling.
- **Status alone does not classify the rest.** A validation error arrives at
  200 with an `errors` array and **no `extensions` key at all**; a bad cursor
  arrives at 200 with `extensions.code == "CursorException"` *and* a populated
  `data`; auth failure at 401; a malformed request at 400. So `errors` is
  parsed on every response, and a missing `extensions` must not throw.

Paging is a cursor on `items_page` itself, with `cursor: null` marking the last
page (`limit` caps at 500). Write-back resolves the operator's configured label
against the board's **real** labels before writing, and never sends
`create_labels_if_missing` — so a typo'd config fails loudly instead of
silently adding a new status to the operator's board.

Polling, not webhooks, for the same reason as Linear: a receiver needs a
publicly reachable HTTPS endpoint and no_human binds to `127.0.0.1`.

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
uv run nh task context <id>   # gather + show, no implementation run
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

### What resolves for GitLab, and what does not

`vcs/pr_watcher.py` is only partly forge-neutral, and the difference is silent
in both directions — every unsupported call returns an EMPTY value, which is
also what "no data yet" looks like. A GitLab operator therefore has no way to
tell "this MR has no failing checks" from "checks are never read here".

| function | GitHub / GHE | GitLab |
|---|---|---|
| `default_pr_state` / `default_pr_merged` | `gh pr view` | `glab api` — supported |
| `check_pr_comments` / `post_reply_comment` / `upsert_agent_comment` | `gh` | `glab` — supported |
| `default_pr_checks` | `gh` statusCheckRollup | **not implemented** — always `[]` |
| `default_pr_head` | `gh pr view --json headRefOid` | **not implemented** — always `""` |
| `default_pr_mergeable` | `gh pr view --json mergeable` | **not implemented** — always `{"mergeable": "", "mergeStateStatus": ""}` |
| `default_pr_files` | `gh pr view --json files` | **not implemented** — always `[]` |

The four unimplemented ones early-return on `shutil.which("gh")` and then parse
the ref through a GitHub-only helper, so a `project!iid` or a GitLab MR URL
never reaches a query. This bounds the awaiting-approval watcher: on GitLab it
wakes on merge and on comments, but it cannot see a red pipeline on the MR.
Use the `ci.*` gate (below) for that — it is `glab`-native and does resolve.

MR refs accept GitLab's own `group/project!7` as well as the pre-encoded
`group%2Fproject!7`; both are normalized by `gitlab_project_path` before the
API call, because GitLab 404s on a raw slash in the project path.

## CI (`no_human/ci/`)

Opt-in per project (`ci.enabled`). Five backends, selected by `ci.backend`.
The **identifier in the left column is the literal string** `ci.backend` takes;
each backend reads a different set of `ci.*` keys, and a key another backend
needs is ignored rather than rejected, so getting the set wrong produces a
backend that builds and then fails against the real service.

| `ci.backend` | required keys | credentials (`~/.no_human/.env`) |
|---|---|---|
| `gitlab` | `project` (`group/subgroup/repo`), `hostname` | `GITLAB_TOKEN` |
| `github_actions` | `repo` (`org/repo`), `workflow` | `gh auth login`, or `GH_ENTERPRISE_TOKEN` for GHE |
| `jenkins` | `job` (path form, e.g. `job/folder/job/main`), `base_url` | `JENKINS_USER` + `JENKINS_API_TOKEN`; `SSO_USERNAME` + `SSO_PASSWORD` for `auth: cookie` |
| `circleci` | `project` — a **project slug** `<vcs>/<org>/<repo>`, e.g. `gh/acme/svc` | `CIRCLECI_TOKEN` |
| `ghe_checkruns` | `repo`, `hostname` | `GH_ENTERPRISE_TOKEN` |

Shared keys, all optional: `mode` (`watch` — poll the pipeline your push
already started, the default for `jenkins` and `circleci` — or `trigger`, which
starts one), `timeout_minutes` (60), `poll_interval` (30), `max_infra_retries`
(2), `result_parser` (`pytest`, or `surefire` for Maven), `variables`.

Jenkins-only keys: `auth` (`token`, the default basic-auth mode, or `cookie`
for controllers that reject API-token basic auth), `crumb_path`
(`crumbIssuer/api/json`), `storage_state_path`, `cookie_auto_refresh`,
`wake_hint`.

> **CircleCI's `project` is not the GitLab `project`.** It is the slug
> `<vcs>/<org>/<repo>` (`gh/acme/svc`), not a `group/subgroup/repo` path.
> Copying the GitLab sample below builds a backend that then 404s on every
> call.

### How a CI result gates the loop

CI runs **last**, after review, the tamper guard and the local suite have all
passed, and the branch is pushed first so the pipeline can see it. The verdict
routes four ways, and the difference matters because only one of them is the
coder's problem:

| verdict | what happens |
|---|---|
| passed | proceed to open the PR |
| failed (real) | back to implement within `max_attempts`, with the failing jobs as evidence |
| `infra_failure` | retry after 120 s, up to `max_infra_retries`, then escalate |
| `access_failure` | park with a `MISSING_ACCESS` blocker naming the exact `.env` key |
| `HumanGatedCI` | park with a wake condition — a human must start this pipeline |

**A misconfigured CI escalates before the first metered call** (closed
2026-08-02; [KNOWN_ISSUES.md](KNOWN_ISSUES.md) KI-5). If `ci.enabled` is true
but no backend can be built — a missing required key, or a misspelled
`ci.backend` — `ci_from_config` raises `CIMisconfigured` rather than returning
`None` (`ci/__init__.py`), and `Orchestrator._drive` raises an `IMPOSSIBLE`
blocker (`blockers.ci_misconfigured`) that routes to ESCALATED + notify. The
run stops; it does not open an ungated PR. The `advisory` event naming the
source and the reason, and `nh doctor`'s `CI BACKEND UNUSABLE`, are still
emitted alongside it.

Two cases deliberately do NOT escalate, because neither asks for a gate:
`ci.enabled: false` (and no `ci:` block at all) proceeds on the local suite,
and a task kind that cannot open a PR — a standalone code review, an
investigation — is not parked for a gate it was never going to reach.

### GitLab backend detail

- **trigger** `glab api --hostname {host} --method POST projects/{enc}/pipeline
  --input body.json` with body `{"ref": {b}, "variables": [{"key","value"}…]}`
  (`glab ci run` is broken on gitlab.acme.net: defaults to gitlab.com →
  401, drops variables)
- **poll** `glab api projects/{enc}/pipelines/{id}` + `.../jobs`
- **infra vs real** failure discrimination → infra auto-retries (120 s, max 2),
  real failures loop back to implement within `max_attempts`.
- **auth wall vs infra**: `glab`'s stderr is classified only after every echoed
  URL span and every argv token is blanked, so an operator's own project or
  host name can never synthesize a wall out of a network blip.
- **result parsers**: `pytest` summary, Maven `surefire` (`Tests run: X, …`).

**Known gap, unproven in either direction: a 403 arrives as a 404.** GitLab
hides the existence of a project a token cannot see, so an insufficiently
scoped `GITLAB_TOKEN` is reported as `glab: 404 Not Found (HTTP 404)`. That
matches none of the auth signals, so it is classified as infra: the run waits
out both 120 s retries and then parks as `TRANSIENT_INFRA` — a "retry later"
that will never clear — instead of a `MISSING_ACCESS` blocker naming
`GITLAB_TOKEN`. Treating 404 as a wall was considered and rejected: it is also
what a typo'd `ci.project` returns, and that would park a human on a config
error the retry loop otherwise surfaces plainly. **Neither half of this has
been reproduced against a live GitLab** — no valid token was available and
every probe returned 401 before reaching a 404 — so read it as the documented
consequence of the classification rules, not as an observed failure. If you
hit `TRANSIENT_INFRA` on a GitLab pipeline that never starts, check the token's
scope on the project before anything else.

Add a backend by implementing the `ci/base.py` contract and wiring it in
`ci/__init__.py:ci_from_config`.
