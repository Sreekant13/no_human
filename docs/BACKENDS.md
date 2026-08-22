# Coding backends

no_human's implementer ("coder") runs on a **coding backend**. There are three.
The default is unchanged and always will be the default unless you change it.

| | `claude` (default) | `codex` | `local` |
|---|---|---|---|
| Harness | Claude Agent SDK → `claude` CLI | `codex exec --json` → `codex` CLI | Claude Agent SDK → `claude` CLI, pointed at your own server |
| Credential | `CLAUDE_CODE_OAUTH_TOKEN` (subscription) or `ANTHROPIC_API_KEY` (BYO) | `OPENAI_API_KEY` (BYO **only**) | `ANTHROPIC_API_KEY`, per-subprocess only (see below) |
| Model | `llm.primary_model` | `llm.codex_model` | `llm.local_model` |
| Install | `npm install -g @anthropic-ai/claude-code` | `npm install -g @openai/codex` | none — reuses the Claude CLI |

Everything except the coder — reviewer, planner, supervisor, utility, intake —
stays on Claude regardless of this setting. The review gate and the four model tiers are
fixed by the project's non-negotiable constraints, and the amendment that
sanctioned a second *coding* backend moved none of them. So selecting `codex`
means your run bills **two** vendors: OpenAI for the implementer, Anthropic for
everything else. Both credentials must be present.

## Switching

For every task, in config — or for ONE task, on the task: `nh task add … --backend codex`
(or `"backend": "codex"` on `POST /api/tasks`). The per-task value wins for that
task's coder only and gets the same credential/CLI preflight as the global key.

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

## CLI compatibility is probed, never assumed

Verified live against **codex-cli 0.149.0**: `--ask-for-approval` — the flag
this backend used to hardcode — is no longer on `codex exec` at all (it
survives only on the root `codex` command); `codex exec --help` on that build
lists `-c, --config <key=value>` instead. Passing the old flag to `exec` is
`unexpected argument '--ask-for-approval'`, rc=2, before a single turn runs —
a probe verified the pre-fix argv reproduces exactly that.

So the non-interactive approval flag is resolved from the **installed CLI's
own `codex exec --help` output**, at the time the command is built — never
hardcoded, never assumed from a version string. The ladder, in order:

1. `--ask-for-approval never`, when `exec --help` still documents it (older CLIs).
2. `--config approval_policy="never"`, when it doesn't — verified live to be
   syntactically accepted by codex-cli 0.149.0's `codex exec` and to actually
   suppress the interactive prompt (a control run with an unknown config key
   fails with `unknown configuration field`, so the flag is confirmed
   *recognised*, not merely silently ignored).

If neither is present in the installed CLI's help text, the backend refuses to
launch rather than risk an indefinite hang on an approval prompt nobody can
answer — it never falls back to `--dangerously-bypass-approvals-and-sandbox`
or any other flag that removes the sandbox boundary. `nh doctor` runs the same
probe (whenever `worker.backend: codex`, or a live task asks for it) and prints
a `codex backend` row naming the CLI path, the verified version, which
approval mode was chosen (or `UNSUPPORTED`), and `OPENAI_API_KEY` presence
(never its value).

**Resuming a thread is probed separately, because its flag surface is
narrower.** `codex exec resume <thread-id>` accepts neither `--cd` nor
`--sandbox` (verified live: `codex exec resume --help` documents neither — a
resumed thread inherits both from the session it is resuming); the backend
omits both on the resume launch and validates that argv against
`codex exec resume --help`, not the non-resume one. A CLI that accepts the
fresh-attempt argv can still reject the resume one, so `nh doctor` checks and
reports both independently rather than assuming compatibility transfers.

**Model entitlement is a separate, unchecked question.** `nh doctor`'s codex
row always carries a note that `llm.codex_model`'s entitlement cannot be
verified without a **billed** call to `/v1/responses` — a model id appearing
in `GET /v1/models` is not the same as being entitled to call it on
`/v1/responses`, and no diagnostic here spends OpenAI quota to find out. If
the configured model 404s at run time, that surfaces as an ordinary attempt
failure, not a doctor contradiction.

## Codex is BYO-API-key only — a conservative choice, not a known prohibition

An earlier version of this section asserted that OpenAI's terms forbid using
a ChatGPT sign-in to drive a third-party service. That claim was never
sourced and is withdrawn.

What OpenAI's own documentation says, quoted from
[`developers.openai.com/codex/auth`](https://developers.openai.com/codex/auth)
(308-redirects to
[`learn.chatgpt.com/docs/auth`](https://learn.chatgpt.com/docs/auth)),
fetched 2026-08-22: "Codex supports two ways for a person to sign in ...
Sign in with ChatGPT for subscription access [and] Sign in with an API key
for usage-based access," and "The ChatGPT desktop app, Codex CLI, and IDE
extension support both sign-in methods for local work." A ChatGPT sign-in is
therefore an officially documented Codex CLI method. But the same page also
says: "Use API key authentication for programmatic Codex CLI workflows, such
as CI/CD jobs" — closer to what no_human does, since it drives the CLI
unattended.

Whether a third-party tool may drive that ChatGPT sign-in on a user's behalf
is still open: [`openai/codex` discussion
#8338](https://github.com/openai/codex/discussions/8338) asked exactly this,
and an OpenAI maintainer answered only the licensing half, leaving the
policy half unresolved.

So no_human takes the conservative path pending legal advice, not as a
finding of law — a lawyer should settle this, and the answer may well be
that a subscription path is fine. Until then, no_human has **no**
Codex-on-a-ChatGPT-subscription mode: no browser login, no reuse of an
existing `codex login`, no routing of anyone's consumer plan. The CLI is
invoked with `preferred_auth_method="apikey"` precisely so it cannot fall
back to a ChatGPT credential that happens to be on the machine. If you want
that mode today, you want a different tool.

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

## `local` — your own model server

`local` is still the Claude Agent SDK harness (the same `claude` CLI the
default backend runs) — only three environment variables change what it talks
to. It is for a self-hosted or third-party server that speaks the Anthropic
`/v1/messages` API, not a different agent loop.

```yaml
# ~/.no_human/config.yaml
worker:
  backend: local
llm:
  local_model: <the model id the local server exposes>   # REQUIRED, no default
  local_base_url: http://localhost:8000                  # REQUIRED, no default
  local_cli_path: null                                    # null ⇒ the SDK-bundled CLI
```

```bash
# ~/.no_human/.env  (chmod 600, gitignored) — only if your server enforces a key
LOCAL_LLM_API_KEY=whatever-your-server-expects
```

**The child process env is exactly three entries**, injected into that one
subprocess's environment only — never into `os.environ`, never into any other
role's session (reviewer/planner/supervisor/utility all stay on Claude
regardless of this setting, per `CLAUDE_PINNED_ROLES`):

| Variable | Value |
|---|---|
| `ANTHROPIC_BASE_URL` | `llm.local_base_url`, verbatim |
| `ANTHROPIC_API_KEY` | `LOCAL_LLM_API_KEY` from `~/.no_human/.env` if set, else the literal `no-key-local-backend` |
| `CLAUDE_CODE_OAUTH_TOKEN` | explicitly set to `""` |

That last line is deliberate, not incidental: a local run must never carry your
real subscription/enterprise OAuth token to a third-party server, so it is
overridden to empty rather than left to whichever credential the CLI happens to
prefer.

**`llm.local_base_url` is validated, not trusted, before any subprocess
starts** (`config.assert_local_backend_mode`):

- it must be set — an ambient `ANTHROPIC_BASE_URL` in your shell is scrubbed
  and never used as a fallback;
- `http`/`https` only;
- the host must be `localhost` or a **literal** loopback/RFC1918 IP address —
  a DNS name is refused even if it currently resolves to one, because a name is
  resolved again at connect time, which is a rebinding surface;
- a public/routable IP is refused — local mode must not leave the machine;
- no userinfo credentials embedded in the URL (`http://user:pass@host` is
  refused) — if your server needs a key, it goes in `.env` as
  `LOCAL_LLM_API_KEY`, never in the URL or in config.yaml.

**Honest limits.** This is still your own local model, not Claude, wearing the
Claude harness:

- answer quality is entirely the local model's, not Anthropic's — no_human does
  not evaluate or curate it;
- `thinking_budget` is off (`BackendCapabilities.thinking_budget=False`) —
  extended-thinking wiring is Anthropic-specific and a third-party server has
  no reason to implement it;
- `cache_creation_accounting` is off — most local servers do not bill or report
  prompt-cache writes the way Anthropic's API does, so that figure is not
  tracked rather than reported as zero;
- nothing here is billed to Anthropic — but `core/pricing.py` has no published
  price for an arbitrary local model id, so cost figures for `local` runs price
  at the conservative unknown-model fallback and the model id is named in
  `unknown_pricing_models()`;
- only loopback/RFC1918 addresses are accepted — there is no supported way to
  point `local` at a remote/hosted server; that is what `claude`'s BYO-API-key
  mode or `codex` are for;
- the reviewer, planner, supervisor and utility tiers are unaffected — only
  `role="coder"` ever consults `worker.backend` (`resolve_backend_name`) or a
  task's `--backend`, so a `local` run still bills Anthropic for everything
  except the implementer.

## Adding a fourth backend

`agent/backend.py` is the seam: implement `CodingBackend`, declare a
`BackendCapabilities`, add a branch to `make_backend`. The orchestrator is typed
against the protocol and does not import either vendor's SDK on the coder path.
Read the seam module's docstring first — it states which parts of the contract
are load-bearing and why, including the one that is easy to get wrong: an
exception raised by the `on_event` callback must propagate out of `run`, because
that raise is how task cancellation, the budget abort and doom-loop detection
all stop a running attempt.
