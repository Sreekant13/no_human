# Coding backends

no_human's implementer ("coder") runs on a **coding backend**. There are two.
The default is unchanged and always will be the default unless you change it.

| | `claude` (default) | `codex` |
|---|---|---|
| Harness | Claude Agent SDK → `claude` CLI | `codex exec --json` → `codex` CLI |
| Credential | `CLAUDE_CODE_OAUTH_TOKEN` (subscription) or `ANTHROPIC_API_KEY` (BYO) | `OPENAI_API_KEY` (BYO **only**) |
| Model | `llm.primary_model` | `llm.codex_model` |
| Install | `npm install -g @anthropic-ai/claude-code` | `npm install -g @openai/codex` |

Everything except the coder — reviewer, planner, supervisor, utility, intake —
stays on Claude regardless of this setting. The review gate and the four model tiers are
fixed by the project's non-negotiable constraints, and the amendment that
sanctioned a second *coding* backend moved none of them. So selecting `codex`
means your run bills **two** vendors: OpenAI for the implementer, Anthropic for
everything else. Both credentials must be present.

## Switching

```yaml
# ~/.no_human/config.yaml
worker:
  backend: codex          # default: claude
llm:
  codex_model: gpt-5-codex          # default
  codex_reasoning_effort: null      # null ⇒ the CLI's own default
  codex_cli_path: null              # null ⇒ resolve `codex` on PATH
```

```bash
# ~/.no_human/.env  (chmod 600, gitignored)
OPENAI_API_KEY=sk-...
```

The **mode** lives in config; the **key never does**. `nh` refuses to load a
config file containing either vendor's API key, and refuses to start with
`worker.backend: codex` and no `OPENAI_API_KEY` on file.

## There is no subscription path for Codex, deliberately

OpenAI's terms prohibit using ChatGPT to power third-party services. no_human
therefore has **no** Codex-on-a-ChatGPT-subscription mode: no browser login, no
reuse of an existing `codex login`, no routing of anyone's consumer plan. The
CLI is invoked with `preferred_auth_method="apikey"` precisely so it cannot fall
back to a ChatGPT credential that happens to be on the machine. If you want that
mode, you want a different tool.

(The Claude path is different because a Claude *subscription* is the operator's
own credential on the operator's own machine — see the auth constraint. That
reasoning does not transfer to OpenAI, and nothing here assumes it does.)

## What you give up by switching

These are not bugs to be fixed later; they are things `codex exec` does not
expose. Each is declared in code as a `BackendCapabilities` field, so the
orchestrator can gate on it rather than pretending.

**1. The safety guard becomes detection, not prevention.**
On the Claude path, `agent/guard.py` runs as a *PreToolUse hook*: a push to a
protected branch, a write to `.env`, an `rm -rf`, a `gh pr merge` is **denied
before it executes**. `codex exec` has no such hook. The same pure policy runs
here on the *observed* event, and a violation kills the session and fails the
attempt — but the command has already run when we see it. Denial events carry
`meta["post_hoc"] = True` so nothing can confuse the two.

The mitigation that *is* real prevention is the sandbox: coder sessions run
`--sandbox workspace-write`, read-only sessions `--sandbox read-only`.
`--dangerously-bypass-approvals-and-sandbox` is never used. But a sandbox
enforces "inside the workspace"; it does not know about `.env` or about which
branches are protected.

**2. No supervisor, no lint feedback, no scope guard.**
All three are PostToolUse hooks. `codex exec` has none, so the orchestrator
switches them off for the attempt and emits a `backend_degraded` event saying
so. It does not report "supervisor active" for a session where nothing
supervised.

**3. No Agent Skills and no named subagents.** Both are Claude Agent SDK
concepts. The coder still has the full local toolset the Codex CLI ships; it
just cannot delegate to `no_human_researcher` or load a `SKILL.md`.

**4. The mid-attempt budget watch can only bite between turns.**
The Claude stream reports usage per assistant message, so a runaway attempt is
aborted mid-flight. Codex reports usage at the end of a turn. The lifetime and
per-attempt ceilings are still enforced; the granularity is coarser, so a single
turn can overshoot before the abort fires.

**5. `max_turns` is enforced by no_human, not by the vendor.** Turns are counted
from the event stream (each command execution or file change is one) and the
session is killed when the ceiling is crossed. Same ceiling, different enforcer.

**6. Cost figures are less precise.** OpenAI has no billed cache-*write* class,
so `cache_creation_tokens` is legitimately 0 rather than unmeasured — but
`core/pricing.py`'s per-model output premium carries published **Anthropic**
prices only. A Codex model id is unknown to it and prices at the conservative
fallback premium, and is reported by `unknown_pricing_models()`. Dollar figures
for Codex runs are estimates with a wider error bar than Claude ones.

## Adding a third backend

`agent/backend.py` is the seam: implement `CodingBackend`, declare a
`BackendCapabilities`, add a branch to `make_backend`. The orchestrator is typed
against the protocol and does not import either vendor's SDK on the coder path.
Read the seam module's docstring first — it states which parts of the contract
are load-bearing and why, including the one that is easy to get wrong: an
exception raised by the `on_event` callback must propagate out of `run`, because
that raise is how task cancellation, the budget abort and doom-loop detection
all stop a running attempt.
