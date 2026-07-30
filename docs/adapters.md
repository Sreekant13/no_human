# Adapters

no_human is a lean single-backend system (one Claude Agent SDK backend, SQLite,
agentic grep — no RAG/pgvector). "Adapters" are the read/write integrations
around that core. They degrade gracefully: a missing CLI or down source is a
best-effort skip, not a crash.

## Intake (`no_human/intake/`)

A source URL or id is detected and routed to an adapter that produces a `Task`
(title, description, acceptance criteria, external id).

| Source | How | Notes |
|--------|-----|-------|
| **TRACKER / Acme** | REST (`{value, display_value}` shape) | HTML acceptance-criteria parsing; verified on a real tracker record |
| **GitHub Issues** | `gh api` | includes `code.example.com` enterprise |
| **GitLab Issues** | `glab api` | `gitlab.acme.net` |
| **Freeform** | `--title` / `--description` / `--criteria` | no external system |

```bash
nh task add PROJ-42 --repo /path/to/repo
nh task add https://code.example.com/org/repo/issues/12 --repo /path/to/repo
nh task add --title "Add greet(name)" --repo /path/to/repo --criteria "returns 'hi, X'"
```

## Context (`no_human/context/`)

Read-only gatherers run in parallel with a per-source timeout; one slow/bad
source can't abort the rest. Completeness is a **binary** named-artifact check,
never a score.

- **codebase** — agentic grep/glob + `git log`, vendored dirs excluded.
- **sessions** — past no_human session memory.
- **Teams / Outlook** — via the Microsoft 365 connector (read-only tokens,
  separate from the write-only Slack webhook).

```bash
nh task context <id>   # gather + show, no implementation run
```

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
