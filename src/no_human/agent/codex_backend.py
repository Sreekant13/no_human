"""The second coding backend: OpenAI Codex, BYO-API-key only.

WHY A SUBPROCESS AND NOT THE OpenAI PYTHON SDK. Constraint §6's "no
re-implemented tools" survived the 2026-08-01 amendment untouched: a second
backend may be added, but not a hand-rolled reimplementation of tools an SDK
already provides. The OpenAI *Responses* API has no filesystem or shell tools
for a local checkout — driving it would mean writing our own Read/Edit/Bash/
Grep/Glob loop, sandbox and patch applier, which is precisely the forbidden
thing. The Codex CLI already ships all of that. So this backend drives
``codex exec --json`` exactly the way ``claude_agent_sdk`` drives the ``claude``
CLI: a subprocess, a JSONL event stream, and a normalizer. It adds ZERO Python
dependencies for the same reason the Claude path needs no ``anthropic`` package.

WHY BYO-API-KEY ONLY, AND NOTHING ELSE. An earlier version of this docstring
asserted, as settled fact, that OpenAI's terms forbid a ChatGPT sign-in from
driving a third-party service. That sentence was never sourced — no such
clause was found in OpenAI's terms — and is withdrawn.

WHAT OPENAI'S OWN DOCUMENTATION SAYS, quoted from
``developers.openai.com/codex/auth`` (308-redirects to
``learn.chatgpt.com/docs/auth``), fetched 2026-08-22:

  * "Codex supports two ways for a person to sign in when using OpenAI
    models: Sign in with ChatGPT for subscription access [and] Sign in
    with an API key for usage-based access."
  * "The ChatGPT desktop app, Codex CLI, and IDE extension support both
    sign-in methods for local work."
  * "Use API key authentication for programmatic Codex CLI workflows,
    such as CI/CD jobs. Don't expose Codex execution in untrusted or
    public environments."

So a ChatGPT sign-in IS an officially documented Codex CLI method for local
work — the first two quotes above contradict the withdrawn sentence for
exactly the case we would use. The THIRD quote is the unfavourable half and
must not be dropped just because it is unfavourable: OpenAI steers
PROGRAMMATIC workflows — CI/CD jobs, nearer what no_human does, since it
drives the CLI unattended with nobody at the keyboard — to the API key, not
to a ChatGPT sign-in.

STILL OPEN, named here as open rather than resolved: whether a THIRD-PARTY
tool may drive that ChatGPT sign-in on a user's behalf. ``openai/codex``
discussion #8338 asked exactly this question; an OpenAI maintainer answered
only the licensing half and left the policy half unresolved — unanswered,
not settled either way.

So BYO-API-key is the deliberately CONSERVATIVE choice under genuine
uncertainty, not a finding of law: the one path whose terms are unambiguous,
taken pending legal advice. A lawyer should settle this, and the answer may
well be that a subscription path is fine. Until then there is NO
subscription path here: no browser login flow, no ``codex login``, no reuse
of a ChatGPT session. The run demands ``OPENAI_API_KEY`` from
``~/.no_human/.env`` (the mode lives in config, the key never does — the
same rule as constraint §1) and passes ``preferred_auth_method=apikey`` so
the CLI cannot silently fall back to a ChatGPT credential that happens to be
on the machine. If the key is absent the backend refuses to start rather
than degrading to whatever auth it finds.
See ``config.assert_codex_api_key_mode``.

WHAT THIS BACKEND CANNOT DO, stated here and declared in
:data:`CODEX_CAPABILITIES` so it is a fact at the seam rather than a surprise:

  * **No PreToolUse veto.** ``codex exec`` has no hook that can deny a proposed
    tool call. ``agent.guard`` is therefore evaluated on the OBSERVED event —
    detection, not prevention. A violation kills the session at the next event
    boundary and fails the attempt, so the guard still has teeth, but the
    offending command has ALREADY RUN when we see it. The sandbox flag
    (``--sandbox read-only`` / ``workspace-write``) is the only true
    prevention available, and it enforces "inside the workspace", not
    "not ``.env``" and not "not a protected branch".
  * **No PostToolUse hooks**, so ``supervisor_hook`` and ``lint_hook`` cannot
    fire. Passing one is an error rather than a silent no-op: a supervisor that
    never runs is worse than no supervisor, because the orchestrator reports
    that it supervised.
  * **No Agent Skills and no named subagents.** Both are Claude Agent SDK
    concepts with no ``codex exec`` equivalent.
  * **Usage arrives at the end of a turn**, not per assistant message, so the
    mid-attempt ``BudgetAbort`` watch can only bite between turns.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from . import guard
from .backend import (
    AgentEvent,
    AgentResult,
    BackendCapabilities,
    BackendUnavailable,
    DEFAULT_CODEX_MODEL,
)

#: Declared once, read by ``nh doctor``, the seam's tests, and anything that
#: wants to know what it is talking to before it talks to it.
CODEX_CAPABILITIES = BackendCapabilities(
    name="codex",
    blocks_tool_calls=False,      # post-hoc guard only — see module docstring
    post_tool_hooks=False,
    session_resume=True,          # `codex exec resume <thread-id>`
    subagents=False,
    skills=False,
    thinking_budget=False,        # effort levels, not a token budget
    incremental_usage=False,      # usage lands on turn.completed
    cache_creation_accounting=False,  # no billed cache-write class at OpenAI
    native_max_turns=False,       # enforced here, from the event stream
)

#: ``effort`` in this codebase is Claude's vocabulary ("low"/"medium"/"high").
#: Codex spells the same knob ``model_reasoning_effort``. Mapped explicitly, so
#: a value neither side knows falls through to the CLI's own default rather
#: than being passed on and rejected.
_EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high"}

#: Codex item types that mean "the agent did something", mapped into the tool
#: vocabulary ``agent.guard`` and the orchestrator already speak. This is a
#: TRANSLATION table, not a tool implementation: the Codex CLI executes the
#: command and applies the patch; we only rename what it reports.
_ITEM_TOOL_NAMES = {
    "command_execution": "Bash",
    "local_shell_call": "Bash",
    "file_change": "Write",
    "patch_apply": "Write",
    "mcp_tool_call": "Mcp",
    "web_search": "WebSearch",
}


class CodexAuthError(RuntimeError):
    """Codex was selected without a usable BYO ``OPENAI_API_KEY``."""


def find_codex_cli(explicit: str | None = None) -> str | None:
    """Resolve the ``codex`` CLI, or None.

    Mirrors ``backend_check.find_claude_cli``'s shape: an explicit configured
    path first, then ``PATH``, then the npm/local install locations. Read-only
    and side-effect free — it never spawns the CLI and never touches the env.
    """
    if explicit:
        return explicit if Path(explicit).is_file() else None
    if cli := shutil.which("codex"):
        return cli
    home = Path.home()
    candidates = [Path("/usr/local/bin/codex"), Path("/opt/homebrew/bin/codex")]
    candidates += [
        home / rel
        for rel in (
            ".npm-global/bin/codex",
            ".local/bin/codex",
            "node_modules/.bin/codex",
            ".bun/bin/codex",
            ".cargo/bin/codex",
        )
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


#: Per-process cache for the two read-only CLI probes below, keyed by
#: resolved CLI path. `_command` runs once per attempt but a single process
#: can launch many attempts, so this keeps each unique CLI path to one
#: `--help`/`--version` subprocess spawn for the life of the process rather
#: than one per attempt. `_HELP_CACHE` is keyed by `(cli, resume)` because
#: `codex exec resume --help` documents a DIFFERENT, narrower flag surface
#: than `codex exec --help` (verified live: no `--cd`, no `--sandbox`) — the
#: two must never share a cache slot.
_HELP_CACHE: dict[tuple[str, bool], str | None] = {}
_VERSION_CACHE: dict[str, str] = {}


def reset_probe_caches() -> None:
    """Test-only: clear the per-path help/version caches between cases."""
    _HELP_CACHE.clear()
    _VERSION_CACHE.clear()


def codex_exec_help(cli: str, *, resume: bool = False, timeout: float = 10.0) -> str | None:
    """Return ``codex exec [resume] --help``'s combined stdout+stderr, or None.

    Read-only, cached per ``(cli, resume)``. This is how :func:`approval_args`
    learns which non-interactive flag the INSTALLED binary actually accepts
    instead of assuming one — codex-cli moved ``--ask-for-approval`` off
    ``exec`` and onto only the root ``codex`` command at some point before
    0.149.0 (confirmed live: ``codex exec --help`` lacks it, ``codex --help``
    has it at ``-a, --ask-for-approval <APPROVAL_POLICY>``), and a hardcoded
    flag silently started failing every Codex-backed attempt at launch.

    ``resume=True`` probes ``codex exec resume --help`` instead: that
    subcommand accepts neither ``--cd`` nor ``--sandbox`` (a resumed thread
    inherits both from the session it is resuming), so the resume argv must be
    validated against its OWN help text, not the non-resume one.
    """
    key = (cli, resume)
    if key in _HELP_CACHE:
        return _HELP_CACHE[key]
    argv = [cli, "exec", "resume", "--help"] if resume else [cli, "exec", "--help"]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        combined = (proc.stdout or "") + (proc.stderr or "")
        text = combined if combined.strip() else None
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        text = None
    _HELP_CACHE[key] = text
    return text


def codex_version(cli: str, *, timeout: float = 10.0) -> str:
    """Return ``codex --version``'s first output line, or a placeholder.

    Read-only, cached per CLI path. Used only for human-facing messages
    (``nh doctor``, the :class:`BackendUnavailable` raised by
    :func:`approval_args`) — never parsed for a version comparison, since the
    flag probe in :func:`codex_exec_help` is the actual source of truth.
    """
    if cli in _VERSION_CACHE:
        return _VERSION_CACHE[cli]
    version = "unknown version"
    try:
        proc = subprocess.run(
            [cli, "--version"], capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        combined = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if combined:
            version = combined.splitlines()[0].strip()
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        pass
    _VERSION_CACHE[cli] = version
    return version


def emitted_flags(cmd: list[str]) -> list[str]:
    """The flag tokens (not their values) a built argv actually emits.

    A lone ``"-"`` (stdin marker, always the last token) is excluded — it is
    a positional argument, not a flag, and ``codex exec --help`` never lists
    it as one.
    """
    return [tok for tok in cmd if tok.startswith("-") and tok != "-"]


def approval_args(help_text: str | None, version: str) -> list[str]:
    """Pick the non-interactive-approval flag the installed CLI accepts.

    Resolved from ``codex exec --help``'s own text — never assumed — because
    the flag moved between CLI versions and a wrong guess is "unexpected
    argument" (rc=2) at launch, before a single turn runs. Preference order:

      1. ``--ask-for-approval never`` when ``exec`` still documents it.
      2. ``-c approval_policy="never"`` (the ``--config`` escape hatch) when it
         doesn't — verified live to be syntactically accepted by codex-cli
         0.149.0's ``codex exec`` and to actually suppress the interactive
         prompt (the run proceeds to real turn execution).

    Deliberately NEVER ``--dangerously-bypass-approvals-and-sandbox`` or
    ``--approve-for-me``-style flags: those remove the ``--sandbox`` boundary
    too, and the sandbox is this backend's only real prevention (module
    docstring) — approval-mode compatibility must never be bought by trading
    it away. If neither known mode is available, raise rather than launch a
    session that could hang forever on an approval prompt nobody can answer.
    """
    text = help_text or ""
    if "--ask-for-approval" in text:
        return ["--ask-for-approval", "never"]
    if "--config" in text or " -c," in text or " -c " in text:
        return ["--config", 'approval_policy="never"']
    raise BackendUnavailable(
        f"the installed `codex` CLI ({version}) exposes neither "
        "`--ask-for-approval` nor a `--config` escape hatch in `codex exec "
        "--help` — there is no way to run it non-interactively without "
        "risking an indefinite hang on an approval prompt (nobody is at the "
        "keyboard). Upgrade or downgrade the CLI (`npm install -g "
        "@openai/codex`), then re-run `nh doctor`."
    )


class CodexModelUnavailable(RuntimeError):
    """``llm.codex_model`` is not entitled on ``/v1/responses`` for this key.

    ``GET /v1/models`` listing a model id is not the same thing as being
    entitled to call it on ``/v1/responses`` — that can only be learned from
    a real, billed call, which this backend never makes speculatively.
    """


#: Substrings that show up in a real vendor "this model id doesn't exist for
#: you" message. Verified live against codex-cli 0.149.0 with an invalid
#: model id: the CLI does NOT surface a clean, single `turn.failed` with a
#: structured `error.status` — it emits repeated flat
#: `{"type": "error", "message": "Reconnecting... N/5 (unexpected status 404
#: Not Found: Model not found <model>)"}` records and retries indefinitely
#: rather than terminating the turn. "does not exist" is kept too for the
#: differently-worded 404 some OpenAI endpoints use for other model
#: mismatches. "not supported when using codex" is a THIRD shape, measured
#: live 2026-08-22: a bad model on a ChatGPT/subscription session returns
#: `turn.failed`, status 400, `invalid_request_error`, message "The '<model>'
#: model is not supported when using Codex with a ChatGPT account" — not a 404
#: at all. Deliberately narrow: `insufficient_quota` and other vendor errors
#: must NOT match any of these (see
#: test_a_vendor_error_becomes_a_failed_attempt_not_a_crash).
_MODEL_NOT_FOUND_PATTERNS = (
    "model not found",
    "model_not_found",
    "does not exist",
    "not supported when using codex",
)


def model_error_from_failure(msg: dict, model: str) -> CodexModelUnavailable | None:
    """Classify a ``turn.failed``/``error`` JSONL record, or return None.

    Handles BOTH the nested ``{"error": {"message", "status", "type"}}``
    shape and the flat ``{"type": "error", "message": "..."}`` shape codex
    actually emits for a 404 (see :data:`_MODEL_NOT_FOUND_PATTERNS`). Pure —
    no I/O — so it stays testable without a subprocess.
    """
    if not isinstance(msg, dict):
        return None
    err = msg.get("error")
    err = err if isinstance(err, dict) else {}
    status = err.get("status") or err.get("code") or msg.get("status")
    message = str(err.get("message") or msg.get("message") or "")
    err_type = str(err.get("type") or "")
    lowered = message.lower()
    if not (
        str(status) == "404"
        or err_type == "model_not_found"
        or any(pattern in lowered for pattern in _MODEL_NOT_FOUND_PATTERNS)
    ):
        return None
    return CodexModelUnavailable(
        f"codex exec rejected the configured model {model!r} (llm.codex_model) "
        f"as not found — vendor message: {message or 'model not found'!r}. "
        "`GET /v1/models` listing a model id is not the same as being "
        "entitled to call it on `/v1/responses`; confirm this OPENAI_API_KEY "
        "can actually call it, then update llm.codex_model."
    )


@dataclass
class _Usage:
    """One turn's token report, already translated into THIS ledger's classes.

    The translation is not cosmetic. Anthropic reports ``input_tokens``
    EXCLUDING cache reads, and bills cache reads as their own class at 0.1x
    (``core.pricing``). OpenAI reports ``input_tokens`` INCLUDING
    ``cached_input_tokens`` as a subset. Adding OpenAI's raw ``input_tokens``
    into ``tokens_used`` while ALSO reporting ``cached_input_tokens`` as
    ``cache_read_tokens`` would charge every cached token twice — once at 1.0
    and once at 0.1 — and the budget gate would fire early on long sessions,
    which are exactly the sessions where caching matters most. So the cached
    share is SUBTRACTED out of the fresh total here.
    """

    tokens_used: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    @classmethod
    def parse(cls, raw: Any) -> "_Usage | None":
        if not isinstance(raw, dict):
            return None
        # Two spellings observed across codex releases; neither is guessed at
        # beyond the name, and an absent key is 0 rather than an exception.
        def _i(*names: str) -> int:
            for n in names:
                v = raw.get(n)
                if isinstance(v, (int, float)):
                    return int(v)
            return 0

        cached = _i("cached_input_tokens", "cache_read_input_tokens")
        total_in = _i("input_tokens", "prompt_tokens")
        out = _i("output_tokens", "completion_tokens")
        if not (cached or total_in or out):
            return None
        # `max(..., 0)`: if a future schema ever reports input EXCLUSIVE of the
        # cached share, subtracting would go negative and a negative token count
        # would corrupt every aggregate downstream. Clamping loses precision on
        # a schema we have not seen; going negative loses correctness on one we
        # have.
        return cls(
            tokens_used=max(total_in - cached, 0) + out,
            output_tokens=out,
            cache_read_tokens=cached,
        )


class CodexBackend:
    """Drives one ``codex exec`` session per call to :meth:`run`.

    Satisfies :class:`~no_human.agent.backend.CodingBackend`. Constructed by
    ``agent.backend.make_backend`` when ``worker.backend`` is ``"codex"``;
    never constructed directly by the orchestrator, which does not know this
    class exists.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_CODEX_MODEL,
        reasoning_effort: str | None = None,
        cli_path: str | None = None,
        forbidden_paths: list[str] | None = None,
        never_push_to: list[str] | None = None,
        readonly: bool = False,
        env: dict[str, str] | None = None,
    ):
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.cli_path = cli_path
        # Same defaults as ClaudeBackend, and mutable for the same reason: the
        # orchestrator rewrites both per task on a reused instance.
        self.forbidden_paths = forbidden_paths or [".env", "secrets/", "*.key", "*.pem"]
        self.never_push_to = never_push_to or ["main", "master", "release/*"]
        self.readonly = readonly
        self._env_override = env

    @property
    def capabilities(self) -> BackendCapabilities:
        return CODEX_CAPABILITIES

    # ---------------------------------------------------------------- env --

    def _child_env(self) -> dict[str, str]:
        """The subprocess environment: exactly one billing path, ours.

        ``config.assert_codex_api_key_mode`` has already put ``OPENAI_API_KEY``
        in the process env and scrubbed the alternate OpenAI routings. This
        re-asserts the requirement at the point of use (a long-lived server may
        have been started before the backend was switched) and strips the
        Anthropic credential from the CHILD's env only — the parent process
        still needs it, because the reviewer, planner, supervisor and utility
        tiers remain Claude even when the coder is Codex.
        """
        env = dict(self._env_override if self._env_override is not None else os.environ)
        key = env.get("OPENAI_API_KEY")
        if not key:
            raise CodexAuthError(
                "the coder backend is 'codex' (worker.backend, or this task's "
                "--backend) but no OPENAI_API_KEY was found. "
                "Codex runs on YOUR OWN OpenAI API key — there is no "
                "subscription path here. Not because one is known to be "
                "forbidden: OpenAI documents 'Sign in with ChatGPT' for local "
                "Codex CLI use, but steers programmatic/CI workflows to an "
                "API key, and whether a third-party tool may drive that "
                "sign-in for you is unresolved. no_human takes the "
                "conservative path until a lawyer settles it. See the module "
                "docstring for the quoted source. Add the key to "
                "~/.no_human/.env (chmod 600):\n"
                "  echo 'OPENAI_API_KEY=sk-...' >> ~/.no_human/.env\n"
                "The key never belongs in config.yaml."
            )
        # A Claude credential in the child's environment cannot bill anything
        # through `codex`, but it can be READ by any command the agent runs.
        # Not exporting it is free and removes the whole question.
        for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
                    "ANTHROPIC_AUTH_TOKEN"):
            env.pop(var, None)
        return env

    # ------------------------------------------------------------ command --

    def _command(
        self, cwd: Path, *, effort: str | None, resume: str | None,
    ) -> list[str]:
        cli = find_codex_cli(self.cli_path)
        if cli is None:
            raise BackendUnavailable(
                "the `codex` CLI is not installed or not on PATH, and "
                "worker.backend is 'codex' — every task would fail at launch. "
                "Install it with: npm install -g @openai/codex  (then verify "
                "with `nh doctor`), or set worker.backend back to 'claude'."
            )
        is_resume = bool(resume)
        cmd = [cli, "exec"]
        if is_resume:
            # `codex exec resume <thread-id>` continues a prior thread.
            cmd += ["resume", resume]
        cmd += ["--json"]
        if not is_resume:
            # `codex exec resume --help` documents neither of these (verified
            # live): a resumed thread inherits its cwd and its sandbox from the
            # session it is resuming, and passing either is `unexpected
            # argument`, rc=2, before the resumed turn runs.
            cmd += ["--cd", str(cwd)]
        cmd += ["--model", self.model]
        if not is_resume:
            # read-only sessions (were any ever routed here) get the sandbox
            # that actually enforces it; the coder gets workspace-write, which
            # is the strongest sandbox compatible with editing the checkout.
            # `danger-full-access` is never used: an unsandboxed agent with no
            # PreToolUse veto has no safety boundary left at all.
            cmd += ["--sandbox", "read-only" if self.readonly else "workspace-write"]
        cmd += [
            # THE CONSERVATIVE-PATH GATE, in code: never fall back to a
            # ChatGPT login that happens to exist on this machine. Not a
            # known legal boundary — see the module docstring — but the
            # deliberate choice while that question is unresolved.
            "--config", 'preferred_auth_method="apikey"',
        ]
        # Nobody is at the keyboard. An approval prompt in a headless run is
        # an indefinite hang, not a safety feature — but the flag that
        # suppresses it moved between CLI versions (codex-cli 0.149.0 dropped
        # `--ask-for-approval` from `exec`), so it is resolved from the
        # INSTALLED binary's own `codex exec [resume] --help`, never assumed.
        # See `approval_args`.
        cmd += approval_args(codex_exec_help(cli, resume=is_resume), codex_version(cli))
        mapped = _EFFORT_MAP.get((effort or "").lower())
        if mapped:
            cmd += ["--config", f'model_reasoning_effort="{mapped}"']
        # The prompt arrives on stdin. Passing it as argv would put a
        # multi-thousand-line task brief through the shell's argument limit and
        # into `ps` output.
        cmd.append("-")
        return cmd

    # ------------------------------------------------------------- events --

    def _guard_events(self, tool_name: str, tool_input: dict,
                      cwd: str | None = None) -> str | None:
        """The guard's verdict on an ALREADY-EXECUTED tool call, or None.

        Same pure policy as the Claude path (``agent.guard.evaluate``) — the
        difference is entirely in the timing, and the caller acts on it by
        killing the session rather than by denying the call. ``cwd`` is the
        session's worktree, for the guard's file-existence questions.
        """
        decision = guard.evaluate(
            tool_name, tool_input,
            forbidden_paths=self.forbidden_paths,
            never_push_to=self.never_push_to,
            readonly=self.readonly,
            cwd=cwd,
        )
        return None if decision.allow else decision.reason

    def _translate(self, msg: dict) -> list[AgentEvent]:
        """One Codex JSONL record → zero or more :class:`AgentEvent`.

        Written against the ``codex exec --json`` envelope (``thread.started`` /
        ``item.*`` / ``turn.completed`` / ``turn.failed``) AND the older
        ``{"msg": {"type": ...}}`` envelope, because both shapes exist in the
        wild across codex releases and neither is verifiable from here. An
        unrecognised record yields NO events rather than raising: a stream that
        dies on an unknown type would take the whole attempt with it, and the
        orchestrator's bounded loop would read a schema drift as a code failure.
        """
        events: list[AgentEvent] = []
        kind = str(msg.get("type") or "")

        # Older envelope: the payload is nested under "msg".
        if not kind and isinstance(msg.get("msg"), dict):
            inner = msg["msg"]
            legacy = str(inner.get("type") or "")
            if legacy in ("agent_message", "agent_message_delta"):
                text = str(inner.get("message") or inner.get("delta") or "")
                return [AgentEvent("text", text=text)] if text else []
            if legacy in ("agent_reasoning", "agent_reasoning_delta"):
                text = str(inner.get("text") or inner.get("delta") or "")
                return [AgentEvent("thinking", text=text)] if text else []
            if legacy == "exec_command_begin":
                cmd = inner.get("command")
                cmd_s = " ".join(cmd) if isinstance(cmd, list) else str(cmd or "")
                return [AgentEvent("tool_use", tool_name="Bash",
                                   tool_input={"command": cmd_s},
                                   meta={"tool_use_id": str(inner.get("call_id") or "")})]
            if legacy == "patch_apply_begin":
                changes = inner.get("changes") or {}
                return [
                    AgentEvent("tool_use", tool_name="Write",
                               tool_input={"file_path": str(p)},
                               meta={"tool_use_id": str(inner.get("call_id") or "")})
                    for p in (changes if isinstance(changes, dict) else [])
                ]
            if legacy == "token_count":
                usage = _Usage.parse(inner.get("info") or inner)
                if usage:
                    return [self._usage_event(usage)]
            return []

        # `thread.started` carries the session id and no user-visible content;
        # it is consumed by the reader loop, which owns `session_id`.
        if kind in ("item.started", "item.completed", "item.updated"):
            item = msg.get("item") or {}
            if not isinstance(item, dict):
                return []
            itype = str(item.get("type") or item.get("item_type") or "")
            item_id = str(item.get("id") or "")
            if itype in ("agent_message", "assistant_message"):
                # Only on completion: `item.updated` streams deltas of the same
                # message, and emitting each one would feed the supervisor and
                # the transcript the same prose several times over.
                if kind == "item.completed":
                    text = str(item.get("text") or "")
                    if text:
                        events.append(AgentEvent("text", text=text))
                return events
            if itype == "reasoning":
                if kind == "item.completed":
                    text = str(item.get("text") or item.get("summary") or "")
                    if text:
                        events.append(AgentEvent("thinking", text=text))
                return events
            tool_name = _ITEM_TOOL_NAMES.get(itype)
            if tool_name is None:
                return []
            if kind == "item.started":
                for tool_input in self._tool_inputs(itype, item):
                    events.append(AgentEvent(
                        "tool_use", tool_name=tool_name, tool_input=tool_input,
                        meta={"tool_use_id": item_id},
                    ))
            elif kind == "item.completed":
                out = item.get("aggregated_output") or item.get("output") or ""
                text = out if isinstance(out, str) else json.dumps(out)
                events.append(AgentEvent(
                    "tool_result",
                    meta={
                        "tool_use_id": item_id,
                        "parent_tool_use_id": None,
                        "is_error": bool(item.get("exit_code")),
                        "result_chars": len(text),
                        "non_text_blocks": 0,
                    },
                ))
            return events

        if kind == "turn.completed":
            usage = _Usage.parse(msg.get("usage"))
            if usage:
                events.append(self._usage_event(usage))
            return events

        return events

    @staticmethod
    def _tool_inputs(itype: str, item: dict) -> list[dict]:
        """The guard-shaped input(s) for one Codex item.

        A ``file_change`` item carries SEVERAL paths in one record, and the
        guard is a per-path policy, so one item legitimately becomes several
        ``tool_use`` events. Doing it any other way — checking only the first
        path — is how a forbidden-path guard ends up passing a patch that
        rewrites ``.env`` in its second hunk.
        """
        if itype in ("command_execution", "local_shell_call"):
            cmd = item.get("command")
            cmd_s = " ".join(cmd) if isinstance(cmd, list) else str(cmd or "")
            return [{"command": cmd_s}]
        if itype in ("file_change", "patch_apply"):
            changes = item.get("changes")
            paths: list[str] = []
            if isinstance(changes, dict):
                paths = [str(p) for p in changes]
            elif isinstance(changes, list):
                for c in changes:
                    if isinstance(c, dict):
                        p = c.get("path") or c.get("file_path")
                        if p:
                            paths.append(str(p))
                    elif isinstance(c, str):
                        paths.append(c)
            return [{"file_path": p} for p in paths] or [{"file_path": ""}]
        if itype == "mcp_tool_call":
            return [{"server": str(item.get("server") or ""),
                     "tool": str(item.get("tool") or "")}]
        return [{}]

    @staticmethod
    def _usage_event(usage: _Usage) -> AgentEvent:
        return AgentEvent("usage", meta={
            "tokens_used": usage.tokens_used,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            # There is no billed cache-WRITE class at OpenAI. 0 here is the
            # true value, not an unmeasured one — see
            # BackendCapabilities.cache_creation_accounting.
            "cache_creation_tokens": 0,
        })

    # ------------------------------------------------------------- stream --

    async def stream(
        self,
        prompt: str,
        *,
        cwd: Path,
        max_turns: int,
        effort: str | None = None,
        resume: str | None = None,
        supervisor_hook: Any | None = None,
        lint_hook: Any | None = None,
        skills: list[str] | None = None,
        thinking: bool = False,
        max_thinking_tokens: int | None = None,
        agents: dict[str, Any] | None = None,
        on_compact: Callable[[str], None] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run one ``codex exec`` session, yielding normalized events.

        The final event is always ``result``, including on every failure path —
        the orchestrator's bounded loop reads that event and nothing else, so a
        backend that raised instead would crash the daemon rather than fail an
        attempt (constraint §5).
        """
        # Unsupported knobs are REFUSED, never silently dropped. Each of these
        # is a control the orchestrator believes is running; a no-op would make
        # `nh watch` report supervision, linting or a skill that never existed.
        unsupported = [
            name for name, value in (
                ("supervisor_hook", supervisor_hook),
                ("lint_hook", lint_hook),
                ("skills", skills),
                ("agents", agents),
            ) if value
        ]
        if unsupported:
            yield AgentEvent("result", text=(
                f"the codex backend cannot honour {', '.join(unsupported)} — "
                f"`codex exec` has no PreToolUse/PostToolUse hook, no Agent "
                f"Skills and no named subagents. Refusing rather than running "
                f"a session that silently drops them. Either disable those "
                f"features for this task or set worker.backend: claude."
            ), meta=_error_meta(stop_reason="unsupported"))
            return

        try:
            cmd = self._command(cwd, effort=effort, resume=resume)
            env = self._child_env()
        except (BackendUnavailable, CodexAuthError) as exc:
            yield AgentEvent("result", text=str(exc),
                             meta=_error_meta(stop_reason="error"))
            return

        session_id: str | None = None
        turns = 0
        totals = {"tokens_used": 0, "output_tokens": 0,
                  "cache_read_tokens": 0, "cache_creation_tokens": 0}
        saw_usage = False
        final_text = ""
        failure = ""
        denials: list[str] = []
        stop_reason: str | None = None
        api_error_status: int | None = None

        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd), env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            pass

        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode(errors="replace").strip()
                if not line or not line.startswith("{"):
                    # `codex exec` interleaves human-readable banner lines with
                    # the JSONL on some versions. Skipping non-JSON is what
                    # makes this parser survive that instead of aborting.
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue

                if msg.get("type") == "thread.started":
                    session_id = str(msg.get("thread_id") or "") or session_id
                elif isinstance(msg.get("msg"), dict) and \
                        msg["msg"].get("type") == "session_configured":
                    session_id = str(msg["msg"].get("session_id") or "") or session_id
                elif msg.get("type") in ("turn.failed", "error"):
                    model_exc = model_error_from_failure(msg, self.model)
                    if model_exc is not None:
                        failure = str(model_exc)
                        api_error_status = 404
                    else:
                        err = msg.get("error") or {}
                        failure = str(
                            (err.get("message") if isinstance(err, dict) else None)
                            or msg.get("message") or "codex reported an error")

                for event in self._translate(msg):
                    if event.kind == "tool_use":
                        turns += 1
                        reason = self._guard_events(
                            event.tool_name or "", event.tool_input or {},
                            cwd=str(cwd))
                        if reason:
                            denials.append(reason)
                            yield AgentEvent(
                                "denied", text=reason,
                                tool_name=event.tool_name,
                                tool_input=event.tool_input,
                                # DETECTION, not prevention: the call already
                                # ran. Marked on the event so no reader can
                                # mistake this for the Claude path's veto.
                                meta={"post_hoc": True},
                            )
                            stop_reason = "guard"
                            failure = failure or (
                                "safety guard violated (post-hoc — the codex "
                                f"backend cannot block a call before it runs): {reason}")
                    elif event.kind == "text":
                        final_text = event.text or final_text
                    elif event.kind == "usage":
                        saw_usage = True
                        for k in totals:
                            totals[k] += int(event.meta.get(k, 0) or 0)
                    yield event
                    if stop_reason == "guard":
                        break

                if stop_reason == "guard":
                    break
                if turns >= max_turns > 0:
                    stop_reason = "max_turns"
                    failure = failure or (
                        f"Reached maximum number of turns ({max_turns})")
                    break
        finally:
            # Kill on EVERY exit path, including the exception `on_event`
            # raises into `run` (CancelRequested / BudgetAbort / StuckAbort).
            # Without this a cancelled attempt leaves a live `codex` writing to
            # the working tree that is about to be diffed and committed.
            if proc.returncode is None:
                proc.kill()
            stderr = b""
            try:
                if proc.stderr is not None:
                    stderr = await asyncio.wait_for(proc.stderr.read(), 5)
            except Exception:  # noqa: BLE001 — draining stderr must never
                stderr = b""  # break the attempt; a timeout here loses a hint.
            with_code = await proc.wait()
            if with_code not in (0, -9) and not failure:
                failure = (stderr.decode(errors="replace").strip()[-2000:]
                           or f"codex exited {with_code}")

        yield AgentEvent(
            "result",
            text=failure or final_text,
            meta={
                "num_turns": turns,
                "is_error": bool(failure),
                "tokens_used": totals["tokens_used"],
                # None, not 0, when nothing reported — the same NULL-vs-zero
                # distinction the Claude path keeps all the way to the column.
                "output_tokens": totals["output_tokens"] if saw_usage else None,
                "session_id": session_id,
                "stop_reason": stop_reason or ("error" if failure else "end_turn"),
                "denials": denials,
                "api_error_status": api_error_status,
                "cache_read_tokens": totals["cache_read_tokens"],
                "cache_creation_tokens": 0,
                # No subagents to roll up; 0 here is the true value.
                "subagent_tokens_used": 0,
                "subagent_cache_read_tokens": 0,
                "subagent_cache_creation_tokens": 0,
                "subagent_count": 0,
                "subagent_floored_count": 0,
                "backend": "codex",
            },
        )

    async def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        max_turns: int,
        effort: str | None = None,
        resume: str | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
        supervisor_hook: Any | None = None,
        lint_hook: Any | None = None,
        skills: list[str] | None = None,
        thinking: bool = False,
        max_thinking_tokens: int | None = None,
        agents: dict[str, Any] | None = None,
        on_compact: Callable[[str], None] | None = None,
    ) -> AgentResult:
        """Run to completion, forwarding each event, return the result.

        Structurally identical to ``ClaudeBackend.run``, including the property
        the orchestrator's three abort controls depend on: ``on_event`` is
        called OUTSIDE any ``except``, so an exception it raises propagates out
        of ``run`` and unwinds the session.
        """
        final = AgentResult(
            final_text="", num_turns=0, is_error=False, tokens_used=0,
            session_id=None, stop_reason=None,
        )
        async for event in self.stream(
            prompt, cwd=cwd, max_turns=max_turns, effort=effort, resume=resume,
            supervisor_hook=supervisor_hook, lint_hook=lint_hook, skills=skills,
            thinking=thinking, max_thinking_tokens=max_thinking_tokens,
            agents=agents, on_compact=on_compact,
        ):
            if on_event is not None:
                on_event(event)
            if event.kind == "result":
                m = event.meta
                final = AgentResult(
                    final_text=event.text,
                    num_turns=int(m.get("num_turns", 0)),
                    is_error=bool(m.get("is_error", False)),
                    tokens_used=int(m.get("tokens_used", 0)),
                    session_id=m.get("session_id"),
                    stop_reason=m.get("stop_reason"),
                    denials=m.get("denials", []),
                    cache_read_tokens=int(m.get("cache_read_tokens", 0)),
                    cache_creation_tokens=int(m.get("cache_creation_tokens", 0)),
                    output_tokens=(
                        None if m.get("output_tokens") is None
                        else int(m["output_tokens"])
                    ),
                    api_error_status=m.get("api_error_status"),
                )
        return final


def _error_meta(*, stop_reason: str) -> dict[str, Any]:
    """Result meta for a run that never started. Every key the orchestrator's
    result handler reads is present — a missing one there is a KeyError inside
    the attempt loop, i.e. a crash instead of a failed attempt."""
    return {
        "num_turns": 0,
        "is_error": True,
        "tokens_used": 0,
        "output_tokens": None,
        "session_id": None,
        "stop_reason": stop_reason,
        "denials": [],
        "api_error_status": None,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "subagent_tokens_used": 0,
        "subagent_cache_read_tokens": 0,
        "subagent_cache_creation_tokens": 0,
        "subagent_count": 0,
        "subagent_floored_count": 0,
        "backend": "codex",
    }
