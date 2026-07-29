"""The single coding backend: a thin wrapper over the Claude Agent SDK.

Constraints honoured here:
  - Auth is set up by config.assert_subscription_mode() before we ever run: the
    default subscription mode exports CLAUDE_CODE_OAUTH_TOKEN and scrubs every
    metered var; operator-authorized BYO-API-key mode (llm.auth_mode: "api_key")
    leaves the operator's own ANTHROPIC_API_KEY in the env and scrubs the rest.
    Either way the SDK reads exactly one credential from the environment.
  - The SDK ships Read/Edit/Bash/Grep/Glob — we do NOT re-implement tools (§3.6).
  - A PreToolUse hook enforces the safety guard (forbidden paths, protected
    branches, rm -rf, no merge).
  - We stream events so `nh watch` can render tool calls live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeAgentOptions,
    HookContext,
    HookMatcher,
    ResultMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from . import guard
from .supervisor import SupervisorHook

# Phase 7c: explicit cap for tool-result display. Silent truncation makes the
# model treat a partial as complete; the marker + retrieval hint prevent that.
_TOOL_RESULT_CAP = 2000


def _result_size(content: Any) -> dict[str, Any]:
    """Size of a tool result as the MODEL sees it, not as Python reprs it.

    `ToolResultBlock.content` is `str | list[dict] | None`. The first version used
    `str(content)`, so `[{'type': 'text', 'text': 'hello world'}]` recorded 41 chars
    for 11 of payload (~30 chars of fixed dict-repr overhead, and `\n` counted as two),
    and `content=None` recorded 4 ("None") rather than 0 — planting phantom mass at the
    low end of the very distribution this exists to produce. The threshold is read off
    the HIGH end where that overhead is proportionally small, so the headline survives,
    but any median or percentile taken from repr lengths is wrong.
    """
    non_text = 0
    if content is None:
        text = ""
    elif isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                # `or ""` guards a malformed `{"type":"text","text":null}`: a raise
                # here does NOT just lose telemetry — the nearest handler terminates
                # the stream and fails the whole attempt as an SDK error. This file
                # already states the principle at the guard-hook: telemetry must
                # never break the session.
                if b.get("type") == "text" or "text" in b:
                    parts.append(str(b.get("text") or ""))
                else:
                    # An image block carries a large base64 payload and ZERO text.
                    # Counting it as 0 chars is the SAME defect class as the repr
                    # inflation this helper replaced, in the opposite direction and
                    # an order of magnitude larger (11->41 vs ~1.2M->0). Flagged so
                    # it can be EXCLUDED, the way is_error and parent_tool_use_id are.
                    non_text += 1
            else:
                parts.append(str(b))
        text = "".join(parts)
    else:
        text = str(content)
    return {
        "result_chars": len(text),
        "over_cap": len(text) > _TOOL_RESULT_CAP,
        # A threshold read off the GLOBAL distribution must exclude these; a
        # threshold read off the Bash slice is unaffected, since Bash is text-only.
        "non_text_blocks": non_text,
    }


@dataclass
class AgentEvent:
    """A normalized streaming event for the TUI / logs."""

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
    # HTTP status of the failing API call (429/529/500...). The SDK sets it on
    # the result event precisely when is_error is true and the subtype is
    # "success" — i.e. THIS incident's shape. It was captured on the event but
    # never reached here, so `_classify_error`'s 429/529 branch could not fire:
    # `getattr(result, "api_error_status", None)` was permanently None. The
    # structured twin of the free-text reason this change surfaces.
    api_error_status: int | None = None


def _make_guard_hook(
    forbidden_paths: list[str], never_push_to: list[str], *, readonly: bool = False,
) -> Callable[..., Awaitable[dict]]:
    """Build a PreToolUse hook callback that applies the pure guard policy."""

    async def hook(input_data: dict, tool_use_id: str | None, context: HookContext):
        decision = guard.evaluate(
            input_data.get("tool_name", ""),
            input_data.get("tool_input", {}) or {},
            forbidden_paths=forbidden_paths,
            never_push_to=never_push_to,
            readonly=readonly,
        )
        if decision.allow:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": decision.reason,
            }
        }

    return hook


def _make_compact_hook(on_compact: Callable[[str], None]) -> Callable[..., Awaitable[dict]]:
    """Build a PreCompact hook: pure telemetry, never blocks. Compaction had
    never been OBSERVED for coder sessions (they end ~160k tokens, under the
    CLI's auto-compact threshold) — this makes every firing visible (C1a)."""

    async def hook(input_data: dict, tool_use_id: str | None, context: HookContext):
        try:
            on_compact(str((input_data or {}).get("trigger") or "auto"))
        except Exception:  # noqa: BLE001 — telemetry must never break the session
            pass
        return {}

    return hook


class ClaudeBackend:
    """Drives one Agent SDK session per call to :meth:`run`."""

    def __init__(
        self,
        *,
        model: str,
        forbidden_paths: list[str] | None = None,
        never_push_to: list[str] | None = None,
        permission_mode: str = "bypassPermissions",
        readonly: bool = False,
        supervisor_hook: SupervisorHook | None = None,
        lint_hook: Any | None = None,
    ):
        self.model = model
        self.forbidden_paths = forbidden_paths or [".env", "secrets/", "*.key", "*.pem"]
        self.never_push_to = never_push_to or ["main", "master", "release/*"]
        # bypassPermissions: unattended autonomy. The PreToolUse guard is the
        # real safety boundary and fires even in this mode (Part 10).
        self.permission_mode = permission_mode
        self.readonly = readonly
        self.supervisor_hook = supervisor_hook
        self.lint_hook = lint_hook

    def _options(
        self, cwd: Path, max_turns: int, *, effort: str | None = None,
        resume: str | None = None,
        supervisor_hook: SupervisorHook | None = None,
        lint_hook: Any | None = None,
        skills: list[str] | None = None,
        thinking: bool = False,
        max_thinking_tokens: int | None = None,
        agents: dict[str, AgentDefinition] | None = None,
        on_compact: Callable[[str], None] | None = None,
    ) -> ClaudeAgentOptions:
        hooks: dict = {
            "PreToolUse": [
                HookMatcher(
                    matcher=None,
                    hooks=[
                        _make_guard_hook(
                            self.forbidden_paths,
                            self.never_push_to,
                            readonly=self.readonly,
                        )
                    ],
                )
            ]
        }
        # PostToolUse may carry two callbacks: the deterministic per-edit lint
        # hook (cheap, runs first) and the supervisor's every-N LLM check.
        sv = supervisor_hook or self.supervisor_hook
        lh = lint_hook or self.lint_hook
        post_hooks = []
        if lh is not None:
            post_hooks.append(lh.hook)
        if sv is not None:
            post_hooks.append(sv.hook)
        if post_hooks:
            hooks["PostToolUse"] = [HookMatcher(matcher=None, hooks=post_hooks)]
        if on_compact is not None:
            hooks["PreCompact"] = [
                HookMatcher(matcher=None, hooks=[_make_compact_hook(on_compact)])
            ]
        kwargs: dict[str, Any] = {}
        if thinking:
            # The SDK's `thinking` is a dict (ThinkingConfig), not a bool. Passing
            # True made subprocess_cli do True["type"] → "'bool' object is not
            # subscriptable", crashing EVERY thinking-enabled (complex) task
            # (task 6cfdb936, all attempts). Enabled with a budget, else adaptive.
            kwargs["thinking"] = (
                {"type": "enabled", "budget_tokens": max_thinking_tokens}
                if max_thinking_tokens
                else {"type": "adaptive"}
            )
        if agents:
            kwargs["agents"] = agents
        if skills or not self.readonly:
            # Project scope ONLY, for every writing (coder) session and any
            # session with skills. Left unset, the SDK defaults skill sessions
            # to ["user", "project"] — the operator's plugins, personal
            # settings, and EVERY ~/.claude skill in the coder's per-turn
            # context — and skill-less sessions to NO sources at all (so the
            # target repo's CLAUDE.md wouldn't load). Relevant user skills are
            # copied into the working tree by the orchestrator instead.
            # Read-only sessions (reviewer/planner/supervisor) stay hermetic.
            kwargs["setting_sources"] = ["project"]
        return ClaudeAgentOptions(
            model=self.model,
            cwd=str(cwd),
            max_turns=max_turns,
            permission_mode=self.permission_mode,
            effort=effort,
            resume=resume,
            hooks=hooks,
            skills=skills or None,
            **kwargs,
        )

    async def stream(
        self,
        prompt: str,
        *,
        cwd: Path,
        max_turns: int,
        effort: str | None = None,
        resume: str | None = None,
        supervisor_hook: SupervisorHook | None = None,
        lint_hook: Any | None = None,
        skills: list[str] | None = None,
        thinking: bool = False,
        max_thinking_tokens: int | None = None,
        agents: dict[str, AgentDefinition] | None = None,
        on_compact: Callable[[str], None] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run the agent, yielding normalized events; the final event is ``result``."""
        options = self._options(cwd, max_turns, effort=effort, resume=resume,
                                supervisor_hook=supervisor_hook, lint_hook=lint_hook,
                                skills=skills, thinking=thinking,
                                max_thinking_tokens=max_thinking_tokens,
                                agents=agents, on_compact=on_compact)
        # The SDK signals terminal conditions (notably hitting max_turns) by
        # *raising* a bare Exception from inside query(). It usually emits a
        # ResultMessage first ("agent done: N turns") and THEN raises, so we
        # cannot simply re-raise once a result was seen — that's exactly the
        # max_turns crash. If the raise escaped it would crash the whole
        # orchestrator and never reach the bounded-loop retry/escalate path
        # (constraint #5). So we never let it escape: we emit a corrective
        # is_error result event. run() keeps the LAST result event, so this
        # supersedes any prior (non-error) ResultMessage and the orchestrator
        # treats the attempt as failed rather than crashing or committing
        # half-finished work.
        last_turns = last_tokens = 0
        # The CLI's OWN explanation, carried on the last result event. The
        # SDK then replaces the trailing ProcessError with
        # "Claude Code returned an error result: <subtype>" — which for a
        # quota rejection is the word "success", the least informative
        # string available — and run() keeps the LAST event, so without
        # this the real reason is overwritten and lost.
        last_result_text = ""
        last_api_error_status: int | None = None
        # Carried onto the corrective error event below. run() keeps the LAST
        # result event, so anything missing there is lost: an attempt that hit
        # max_turns used to record 0 cache-read tokens — the attempts that burn
        # the most reporting nothing at all.
        last_cache_read = last_cache_creation = 0
        last_session: str | None = None
        try:
            async for message in query(prompt=prompt, options=options):
                # Tool RESULTS arrive in a UserMessage, not an AssistantMessage:
                # an assistant message carries the ToolUseBlock (the call), and the
                # result comes back as a user turn. A `ToolResultBlock` branch used to
                # sit inside the AssistantMessage loop below, so it was UNREACHABLE —
                # 0 tool_result events across 35 attempts against 1,497 tool_use — and
                # the `_TOOL_RESULT_CAP` truncation a previous author wrote has never
                # executed once. Verified against the SDK types: UserMessage carries
                # `content: str | list[ContentBlock]` and `tool_use_result`.
                #
                # We emit the SIZE, never the text. PR-024 measured that 72% of an
                # attempt's cost is the conversation re-read every turn, and tool
                # results are the payload — but persisting that text would bloat the DB
                # by ~1,500 results per session AND risk capturing whatever a command
                # printed, including credentials. The size is what the truncation
                # threshold must be chosen from; the text is not needed for it.
                if isinstance(message, UserMessage):
                    blocks = message.content
                    if isinstance(blocks, list):
                        for block in blocks:
                            if isinstance(block, ToolResultBlock):
                                yield AgentEvent(
                                    "tool_result",
                                    meta={
                                        # JOIN KEY — pairs this size with its tool.
                                        "tool_use_id": block.tool_use_id,
                                        # A SUBAGENT's results are re-read in the
                                        # SUBAGENT's context, not the main conversation
                                        # whose 72% re-read cost is the target. Counting
                                        # them undifferentiated inflates the population
                                        # the threshold is chosen from.
                                        "parent_tool_use_id": message.parent_tool_use_id,
                                        # Error results are short and a different
                                        # population; they must be excludable.
                                        "is_error": bool(getattr(block, "is_error", False)),
                                        **_result_size(block.content),
                                    },
                                )
                    continue
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ThinkingBlock):
                            yield AgentEvent("thinking", text=block.thinking)
                        elif isinstance(block, TextBlock):
                            yield AgentEvent("text", text=block.text)
                        elif isinstance(block, ToolUseBlock):
                            # `id` is the JOIN KEY. Without it the tool_result size
                            # distribution cannot be sliced BY TOOL — and the whole
                            # point (PR-024) is that Bash is 62% of calls and is the
                            # unbounded one, so the truncation threshold must be
                            # per-tool. Index-pairing is unsound: one assistant turn
                            # can carry several ToolUseBlocks.
                            yield AgentEvent(
                                "tool_use",
                                tool_name=block.name,
                                tool_input=block.input,
                                meta={"tool_use_id": block.id},
                            )
                    # Per-message usage → a running mid-attempt total in the
                    # orchestrator's sink (B2 #2). Summing these per-call
                    # numbers reproduces the ResultMessage's cumulative
                    # totals, so the running counter and the final ledger
                    # count the same tokens.
                    usage = message.usage or {}
                    if usage:
                        yield AgentEvent(
                            "usage",
                            meta={
                                "tokens_used": int(usage.get("input_tokens", 0))
                                + int(usage.get("output_tokens", 0)),
                                "cache_read_tokens": int(
                                    usage.get("cache_read_input_tokens", 0)),
                                "cache_creation_tokens": int(
                                    usage.get("cache_creation_input_tokens", 0)),
                            },
                        )
                elif isinstance(message, TaskStartedMessage):
                    yield AgentEvent(
                        "subagent_start",
                        text=message.description,
                        meta={
                            "task_id": message.task_id,
                            "task_type": message.task_type,
                            "session_id": message.session_id,
                        },
                    )
                elif isinstance(message, TaskProgressMessage):
                    yield AgentEvent(
                        "subagent_progress",
                        text=message.description,
                        meta={
                            "task_id": message.task_id,
                            "last_tool_name": message.last_tool_name,
                            "session_id": message.session_id,
                        },
                    )
                elif isinstance(message, TaskNotificationMessage):
                    yield AgentEvent(
                        "subagent_done",
                        text=message.summary,
                        meta={
                            "task_id": message.task_id,
                            "status": message.status,
                            "session_id": message.session_id,
                        },
                    )
                elif isinstance(message, ResultMessage):
                    usage = message.usage or {}
                    tokens = int(usage.get("input_tokens", 0)) + int(
                        usage.get("output_tokens", 0)
                    )
                    cache_read = int(usage.get("cache_read_input_tokens", 0))
                    cache_creation = int(usage.get("cache_creation_input_tokens", 0))
                    denials = [str(d) for d in (message.permission_denials or [])]
                    last_turns, last_tokens = message.num_turns, tokens
                    # ONLY from an ERRORED result. Capturing a SUCCESSFUL
                    # run's prose here and prepending it below meant a normal
                    # finish followed by a transport error ("Stream closed")
                    # inherited the agent's own summary — and a summary that
                    # happens to mention "rate limit" or "quota" (routine in
                    # THIS codebase, which is full of quota-handling code) then
                    # tripped `_quota_signal`, parking a healthy task as
                    # PAUSED_QUOTA and aborting its bounded loop. It also fed
                    # variable prose into `stuck.record`, so identical failures
                    # stopped hashing to the same signature and stuck detection
                    # broke. The SDK only synthesises the
                    # "error result: <subtype>" wrapper when the result was
                    # itself an error, so this gate costs the incident nothing.
                    if message.is_error:
                        last_result_text = (message.result or "").strip()
                        last_api_error_status = message.api_error_status
                    last_cache_read, last_cache_creation = cache_read, cache_creation
                    last_session = message.session_id
                    yield AgentEvent(
                        "result",
                        text=message.result or "",
                        meta={
                            "num_turns": message.num_turns,
                            "is_error": message.is_error,
                            "tokens_used": tokens,
                            "session_id": message.session_id,
                            "stop_reason": message.stop_reason,
                            "denials": denials,
                            "api_error_status": message.api_error_status,
                            "cache_read_tokens": cache_read,
                            "cache_creation_tokens": cache_creation,
                        },
                    )
        except Exception as exc:  # noqa: BLE001 — SDK raises bare Exception on terminal errors
            import traceback
            msg = str(exc)
            is_max_turns = "maximum number of turns" in msg.lower()
            # Preserve the traceback for genuine errors — a bare "'bool' object is
            # not subscriptable" with no file:line burned 3 attempts undiagnosably
            # (task 6cfdb936). max_turns is not an error, so it keeps the clean msg.
            tb = "" if is_max_turns else traceback.format_exc()
            text = msg if is_max_turns else f"{msg}\n\n{tb[-3000:]}".strip()
            # Lead with what the CLI actually said. A spend-limit rejection
            # reported only as "returned an error result: success" cost a full
            # day of debugging: every attempt showed turns=1, tokens=0 and an
            # error message that named no cause, while the CLI had already
            # said "You've hit your monthly spend limit".
            # Capped like the traceback beside it: an unbounded result is
            # persisted on the event AND fed to `error_signature`.
            if last_result_text and not is_max_turns:
                text = f"{last_result_text[:4000]}\n\n{text}"
            yield AgentEvent(
                "result",
                text=text,
                meta={
                    "num_turns": last_turns or (max_turns if is_max_turns else 0),
                    "is_error": True,
                    "tokens_used": last_tokens,
                    "session_id": last_session,
                    "stop_reason": "max_turns" if is_max_turns else "error",
                    "denials": [],
                    "api_error_status": last_api_error_status,
                    "cache_read_tokens": last_cache_read,
                    "cache_creation_tokens": last_cache_creation,
                    "traceback": tb[-4000:] or None,
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
        supervisor_hook: SupervisorHook | None = None,
        lint_hook: Any | None = None,
        skills: list[str] | None = None,
        thinking: bool = False,
        max_thinking_tokens: int | None = None,
        agents: dict[str, AgentDefinition] | None = None,
        on_compact: Callable[[str], None] | None = None,
    ) -> AgentResult:
        """Run to completion, optionally forwarding each event, return the result."""
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
                    api_error_status=m.get("api_error_status"),
                )
        return final
