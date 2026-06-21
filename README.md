# no_human

Autonomous AI software-delivery orchestrator. It drives the **Claude Agent SDK**
on **your Claude subscription** through a plan → implement → review → test → PR
loop, then stops for a human merge decision. **The agent never merges.**

Design source of truth: [`PLAN.md`](PLAN.md). Implementation brief:
[`BUILD.md`](BUILD.md).

## Status — Phase 0 (walking skeleton) ✅

A freeform task runs end-to-end and opens a PR, on subscription auth:

```bash
nh task add --title "Add greet(name)" --repo /path/to/repo \
  --criteria "greet('x') returns 'hello, x'"
nh task list
nh task show <id>
nh watch <id>        # live Textual stream of the agent's tool calls
```

### What works
- **Subscription auth only.** Token loaded from `~/.no_human/.env`
  (`CLAUDE_CODE_OAUTH_TOKEN`). On startup the process env is scrubbed of
  `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / Bedrock+Vertex vars and refuses
  to start if a metered key is present (it would silently bill the API).
- **State machine + bounds.** `pending → context → planning → implementing →
  reviewing → testing → awaiting_approval`, plus off-ramps (blocked,
  awaiting_input, paused_quota, escalated, failed). `max_attempts`, per-attempt
  `max_turns`, stuck detection (same error signature twice → reset context).
- **Deterministic VCS.** The orchestrator owns git (never the LLM): branch,
  commit under a distinct `no_human` identity, push, open PR/MR (GitHub via `gh`,
  GitLab via `glab`, local bare-repo fallback). Honors `never_push_to`; the
  PreToolUse guard blocks `git merge`, force-push, `rm -rf`, and writes to
  forbidden paths.
- **Trust only verifiable signals.** Test-tampering guard blocks any net
  reduction in test count / assertions; local test runner records pass/fail as
  evidence. Advisory self-check (not a gate — the independent reviewer is
  Phase 2).
- **Slack notify** via a write-only webhook (no-op + log if unconfigured).

### Not yet (later phases)
Real intake adapters & context gathering (1) · independent adversarial reviewer
(2) · CI triggering (3) · web board (4) · blocker taxonomy, learning queue, eval
harness (5).

## Develop

```bash
uv sync
uv run pytest -q
uv run nh --help
```

Config lives at `~/.no_human/config.yaml` (auto-generated on first run; the
metered API key never appears there).
