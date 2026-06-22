# Security & safety model

no_human is designed for **unattended** runs, so its safety properties are
correctness requirements, not preferences. They are enforced in code and covered
by tests.

## 1. Subscription auth only — never the metered API

The Claude Agent SDK honours `ANTHROPIC_API_KEY` **over**
`CLAUDE_CODE_OAUTH_TOKEN` when both are present, so a stray key would silently
bill the pay-per-token API. On startup (`config.assert_subscription_mode`):

1. Every metered-auth variable is **scrubbed** from the process environment:
   `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, the
   Bedrock/Vertex set, `GOOGLE_APPLICATION_CREDENTIALS`, `AWS_BEARER_TOKEN_BEDROCK`.
2. If `ANTHROPIC_API_KEY` was present, the process **refuses to start** (exit 2)
   — the scrub already protected this run, but startup aborts so you fix the
   source rather than masking a misconfiguration.
3. The subscription token is loaded from `~/.no_human/.env`
   (`chmod 600`, gitignored, **never** in the repo) with a process-env fallback.

The token is never read from or written to anywhere in the repo, and
`ANTHROPIC_API_KEY` is rejected if it appears in `config.yaml`.

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
- **Independent reviewer**: a fresh-context `claude-sonnet-4-6` subagent told to
  refute "done", producing an evidence-cited pass/fail checklist — **never a
  numeric self-score**. Reviewer crash → fail-closed.
- **Held-out tests**: `tests/held_out/` are run by the orchestrator and given to
  the reviewer as evidence the implementer never saw.
- **CI retry** only on infra failures (max 2); real failures never auto-retry.

## 5. Bounded loop + honest blockers

`max_attempts`, per-attempt `max_turns`, and stuck detection (same error
signature twice → reset context, don't stack corrections). A blocker is **never**
resolved by weakening tests, expanding scope, editing acceptance criteria, or
faking done — the agent makes verifiable progress, parks with a wake condition,
or escalates with a structured report (see [blockers.md](blockers.md)).

## 6. What to review before trusting an unattended run

- `~/.no_human/.env` is `chmod 600` and contains only `CLAUDE_CODE_OAUTH_TOKEN`.
- `nh eval --gate` is green and the red-team suite shows zero tamper / faked-done
  incidents.
- `never_push_to` and `forbidden_paths` match your repo's protected surface.
