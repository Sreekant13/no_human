"""The coding-backend SEAM: what the orchestrator needs, stated once.

Until 2026-08-01 there was exactly one coding backend and no seam at all — the
constraint read "single Claude backend via the Agent SDK (no Agent-A/api-vendor/
local abstraction)". The operator REMOVED that clause to add OpenAI Codex as a
second coding backend, so a vendor abstraction is now sanctioned. This module is
that abstraction, and nothing else: it holds the two data types the orchestrator
already consumed (:class:`AgentEvent`, :class:`AgentResult` — moved here from
``claude_backend`` and re-exported from it, so no import site changed), the
protocol every backend must satisfy, an HONEST capability record per backend,
and the factory that picks one.

WHY THE INTERFACE IS SHAPED LIKE THIS. It was read off the CALLER, not off
either vendor's SDK. Concretely, ``core/orchestrator.py`` depends on:

  * ``await backend.run(prompt, cwd=…, max_turns=…, effort=…, on_event=…)``
    returning an :class:`AgentResult`, and ``backend.stream(...)`` yielding
    :class:`AgentEvent`. ``run`` is the only method the coder path calls.
  * ``on_event`` being invoked SYNCHRONOUSLY between events, and an exception
    raised inside it propagating OUT of ``run``. ``_agent_sink`` raises
    ``CancelRequested`` (``nh task pause``), ``BudgetAbort`` (the mid-attempt
    spend watch) and ``StuckAbort`` (doom-loop) from inside the callback; that
    raise is the ONLY way those three controls stop a running attempt. A
    backend that swallowed callback exceptions would silently disable all three.
  * event kinds ``thinking`` / ``text`` / ``tool_use`` / ``tool_result`` /
    ``usage`` / ``result`` with the meta keys spelled out in
    :class:`AgentEvent`. ``tool_use`` drives the doom-loop detector, the
    edited-file set (which decides what gets committed) and the scope guard;
    ``usage`` drives the mid-attempt budget watch; ``result`` is the ledger row.
  * MUTABLE ``forbidden_paths`` and ``never_push_to`` attributes — the
    orchestrator rewrites both per task (``_apply_task_guard``,
    ``_protect_base_branch``) on a backend instance the worker pool REUSES.
  * a ``model`` attribute, recorded on the attempt row and used to price it.

Everything else the Claude path offers (``skills``, ``agents``, ``thinking``,
``supervisor_hook``, ``lint_hook``, ``resume``) is OPTIONAL in the protocol and
declared per backend in :class:`BackendCapabilities`. A backend that cannot do
one of them must SAY so there rather than accept the argument and ignore it —
an ignored ``supervisor_hook`` is a supervisor that never fires, which is the
"a check nothing observes is not a check" failure this codebase keeps hitting.

WHAT THIS MODULE DOES NOT DO. It does not re-implement tools (constraint §6,
untouched by the amendment): every backend delegates Read/Edit/Bash/Grep/Glob to
the vendor's own agentic harness. It does not choose credentials — that stays in
``config.py``, where the "mode may live in config, the key never does" rule and
the exactly-one-billing-path scrub live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .supervisor import SupervisorHook


@dataclass
class AgentEvent:
    """A normalized streaming event for the TUI / logs.

    ``kind`` is the vendor-independent vocabulary the orchestrator switches on:
    ``thinking``, ``text``, ``tool_use``, ``tool_result``, ``usage``,
    ``result``, ``denied``, and the ``subagent_*`` trio. A backend that cannot
    produce one of them simply never emits it; it must never invent a kind
    outside this list, because ``_agent_sink`` and the web renderers key on it.
    """

    kind: str            # thinking | text | tool_use | tool_result | result | denied
    text: str = ""
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    """The outcome of one agent run."""

    final_text: str
    num_turns: int
    is_error: bool
    tokens_used: int
    session_id: str | None
    stop_reason: str | None
    denials: list[str] = field(default_factory=list)
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    # The OUTPUT share of `tokens_used` above, which is input+output. Output
    # bills ~5x input, and `_usage_quad` has always returned the number — this
    # dataclass was where it got thrown away, so no cost surface downstream
    # could price it. `tokens_used` is unchanged and still means the total;
    # input is `tokens_used - output_tokens`.
    #
    # `None`, not 0, and that distinction is load-bearing all the way to the
    # DB column: 0 asserts "this run emitted no output tokens", which is true
    # of almost no run, whereas None says the usage block never arrived (an
    # errored result, a session that produced no ResultMessage). It is
    # persisted as SQL NULL and priced at the old input rate — an under-count
    # that is at least visible as unknown rather than asserted as free.
    output_tokens: int | None = None
    # HTTP status of the failing API call (429/529/500...). The SDK sets it on
    # the result event precisely when is_error is true and the subtype is
    # "success" — i.e. THIS incident's shape. It was captured on the event but
    # never reached here, so `_classify_error`'s 429/529 branch could not fire:
    # `getattr(result, "api_error_status", None)` was permanently None. The
    # structured twin of the free-text reason this change surfaces.
    api_error_status: int | None = None
    # The subagent (Task tool) share of the three totals above, which INCLUDE
    # it. Broken out so a reader can see the ledger grew for a reason. These
    # ride the "result" AgentEvent into the orchestrator's event stream; no
    # surface reads them back out of the DB yet, so the audit trail is the
    # event log, not a column.
    subagent_tokens_used: int = 0
    subagent_cache_read_tokens: int = 0
    subagent_cache_creation_tokens: int = 0
    subagent_count: int = 0
    # How many of `subagent_count` contributed a FLOOR rather than a
    # measurement — subagents that streamed no assistant message at all, whose
    # only signal is the CLI's context-size gauge (7-17% of real spend). A
    # datum, not a comment: any surface that reports subagent spend can say
    # "N of M are floors" instead of presenting an undercount as a total.
    subagent_floored_count: int = 0


@dataclass(frozen=True)
class BackendCapabilities:
    """What a backend can actually do, declared rather than assumed.

    Every field here corresponds to something the orchestrator PASSES or RELIES
    ON. The point of the record is that a mismatch is a readable fact at the
    seam instead of a silently-ignored keyword argument three layers down. The
    Codex backend is missing several of these and says so; see
    ``docs/BACKENDS.md`` for what each absence costs.
    """

    #: Stable identifier, matching the ``worker.backend`` config value.
    name: str
    #: A PreToolUse decision can BLOCK a tool call before it executes
    #: (``agent.guard.evaluate`` wired as a real veto). False means the guard
    #: can only be evaluated AFTER the fact — detection, not prevention.
    blocks_tool_calls: bool
    #: PostToolUse callbacks fire per tool call, so ``supervisor_hook`` and
    #: ``lint_hook`` can observe and inject a correction mid-session.
    post_tool_hooks: bool
    #: ``resume=<session_id>`` continues a previous session's context.
    session_resume: bool
    #: Named subagents (``agents=``) can be defined for the session.
    subagents: bool
    #: Agent Skills (``skills=``) can be attached to the session.
    skills: bool
    #: An explicit extended-thinking token budget can be set.
    thinking_budget: bool
    #: Usage is reported INCREMENTALLY during the run, not only at the end.
    #: The mid-attempt budget watch (``BudgetAbort``) can only bite when this
    #: is True; with False the ceiling is still enforced, but only between
    #: attempts, so one attempt can overshoot it.
    incremental_usage: bool
    #: The vendor reports cache-write tokens as a separate billed class.
    #: False is not "we failed to measure it" — for OpenAI there is no such
    #: class to measure (prompt caching is automatic and cache writes are not
    #: billed separately), so ``cache_creation_tokens`` is legitimately 0.
    cache_creation_accounting: bool
    #: ``max_turns`` is enforced by the vendor harness itself. False means this
    #: backend counts turns from its own event stream and terminates the
    #: session when the count is exceeded — the same ceiling, enforced by us.
    native_max_turns: bool


@runtime_checkable
class CodingBackend(Protocol):
    """The contract ``core.orchestrator.Orchestrator`` is written against.

    Deliberately narrow. ``run`` is the only method the coder path calls;
    ``stream`` exists because a handful of intake/eval callers consume events
    directly. The three attributes are mutated by the orchestrator between
    tasks and are therefore part of the contract, not implementation detail.
    """

    #: Recorded on the attempt row and used to price it.
    model: str
    #: Rewritten per task by ``Orchestrator._apply_task_guard``.
    forbidden_paths: list[str]
    #: Rewritten per task by ``Orchestrator._protect_base_branch``.
    never_push_to: list[str]

    @property
    def capabilities(self) -> BackendCapabilities: ...

    def stream(
        self,
        prompt: str,
        *,
        cwd: Path,
        max_turns: int,
        effort: str | None = ...,
        resume: str | None = ...,
        supervisor_hook: "SupervisorHook | None" = ...,
        lint_hook: Any | None = ...,
        skills: list[str] | None = ...,
        thinking: bool = ...,
        max_thinking_tokens: int | None = ...,
        agents: dict[str, Any] | None = ...,
        on_compact: Callable[[str], None] | None = ...,
    ) -> AsyncIterator[AgentEvent]: ...

    async def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        max_turns: int,
        effort: str | None = ...,
        resume: str | None = ...,
        on_event: Callable[[AgentEvent], None] | None = ...,
        supervisor_hook: "SupervisorHook | None" = ...,
        lint_hook: Any | None = ...,
        skills: list[str] | None = ...,
        thinking: bool = ...,
        max_thinking_tokens: int | None = ...,
        agents: dict[str, Any] | None = ...,
        on_compact: Callable[[str], None] | None = ...,
    ) -> AgentResult: ...


class BackendUnavailable(RuntimeError):
    """A backend was selected that this install cannot run."""


#: Roles that are PINNED to Claude regardless of ``worker.backend``, and why.
#:
#: This project's non-negotiable constraints fix the review gate as "an
#: independent fresh-context Agent SDK reviewer (claude-opus-5)", and fix all
#: four model tiers
#: (implementer / reviewer+planner / supervisor / utility) by ID. The operator's
#: 2026-08-01 amendment sanctioned a second CODING backend; it did not move the
#: review gate, the planner, the supervisor or the utility tier off Claude, and
#: a config key that silently did so would be a constraint change wearing the
#: costume of a feature. So the switch reaches exactly one role: the coder.
#:
#: This is a denylist of roles rather than an allowlist of one because the
#: default for an unrecognised role must be the SAFE side (stay on Claude), and
#: because `role` is a caller-supplied string — see the `!= "coder"` test below,
#: which is what actually implements that default. The tuple is documentation.
CLAUDE_PINNED_ROLES = ("reviewer", "planner", "supervisor", "utility", "intake")


def resolve_backend_name(config: dict[str, Any] | None, *, role: str = "coder") -> str:
    """Which backend name *role* should use, given a config mapping.

    Only ``role="coder"`` consults ``worker.backend``; everything else resolves
    to ``"claude"``. See :data:`CLAUDE_PINNED_ROLES`.
    """
    if role != "coder":
        return "claude"
    return str(((config or {}).get("worker") or {}).get("backend") or "claude")


def make_backend(
    *,
    model: str,
    config: dict[str, Any] | None = None,
    backend: str | None = None,
    role: str = "coder",
    forbidden_paths: list[str] | None = None,
    never_push_to: list[str] | None = None,
    permission_mode: str = "bypassPermissions",
    readonly: bool = False,
    supervisor_hook: "SupervisorHook | None" = None,
    lint_hook: Any | None = None,
    tool_result_caps: dict[str, int] | None = None,
    codex_config: dict[str, Any] | None = None,
) -> CodingBackend:
    """Build the coding backend for *role*.

    ``backend`` overrides the config lookup (tests, and the ``nh doctor``
    probe). Everything else is passed straight through to the chosen backend's
    constructor with the SAME meaning it has on ``ClaudeBackend`` — the seam
    exists to make the caller not care, so the caller's argument list does not
    change when the backend does.

    Defaulting is the acceptance criterion for this whole change: with no
    config, or with ``worker.backend`` absent or set to ``"claude"``, this
    returns exactly the ``ClaudeBackend`` the caller used to construct
    directly, with identical arguments. An operator who changes nothing sees
    no behavioural difference.
    """
    name = (backend or resolve_backend_name(config, role=role)).strip().lower()

    if name in ("claude", "", "claude-agent-sdk"):
        from .claude_backend import ClaudeBackend

        return ClaudeBackend(
            model=model,
            forbidden_paths=forbidden_paths,
            never_push_to=never_push_to,
            permission_mode=permission_mode,
            readonly=readonly,
            supervisor_hook=supervisor_hook,
            lint_hook=lint_hook,
            tool_result_caps=tool_result_caps,
        )

    if name == "codex":
        from .codex_backend import CodexBackend

        cfg = codex_config if codex_config is not None else (
            (config or {}).get("llm") or {})
        return CodexBackend(
            # The Claude tier IDs in `config.llm` are fixed by the programme's
            # model-tier constraint and
            # are meaningless to Codex, so the Codex model is its OWN key. The
            # `model=` argument is the CLAUDE tier the caller asked for; it is
            # deliberately NOT forwarded.
            model=str(cfg.get("codex_model") or DEFAULT_CODEX_MODEL),
            reasoning_effort=cfg.get("codex_reasoning_effort"),
            cli_path=cfg.get("codex_cli_path"),
            forbidden_paths=forbidden_paths,
            never_push_to=never_push_to,
            readonly=readonly,
        )

    raise BackendUnavailable(
        f"unknown coding backend {name!r}. Supported: 'claude' (default), "
        f"'codex'. Set it with `worker.backend` in ~/.no_human/config.yaml."
    )


#: Default Codex model id. Chosen EXPLICITLY (BUILD item 4) rather than
#: inherited from a Claude tier, and overridable via ``llm.codex_model``.
#: ``gpt-5-codex`` is OpenAI's coding-tuned model and the Codex CLI's own
#: default; naming it here means a future default change is a visible diff
#: rather than a silent drift in someone else's release notes.
#:
#: NOT verified against the live API in this change — see the report's
#: "what I could not check". If OpenAI has retired or renamed this id, the run
#: fails loudly at the first CLI call with the vendor's own error, which is the
#: right failure: an absence, named.
DEFAULT_CODEX_MODEL = "gpt-5-codex"

__all__ = [
    "AgentEvent",
    "AgentResult",
    "BackendCapabilities",
    "BackendUnavailable",
    "CodingBackend",
    "CLAUDE_PINNED_ROLES",
    "DEFAULT_CODEX_MODEL",
    "make_backend",
    "resolve_backend_name",
]
