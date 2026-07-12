"""The single coding backend: a thin wrapper over the Claude Agent SDK.

Constraints honoured here:
  - Subscription auth only (CLAUDE_CODE_OAUTH_TOKEN). The env is scrubbed of
    metered-API vars by config.assert_subscription_mode() before we ever run.
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
    query,
)

from . import guard
from .supervisor import SupervisorHook

# Phase 7c: explicit cap for tool-result display. Silent truncation makes the
# model treat a partial as complete; the marker + retrieval hint prevent that.
_TOOL_RESULT_CAP = 2000


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
        # Carried onto the corrective error event below. run() keeps the LAST
        # result event, so anything missing there is lost: an attempt that hit
        # max_turns used to record 0 cache-read tokens — the attempts that burn
        # the most reporting nothing at all.
        last_cache_read = last_cache_creation = 0
        last_session: str | None = None
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ThinkingBlock):
                            yield AgentEvent("thinking", text=block.thinking)
                        elif isinstance(block, TextBlock):
                            yield AgentEvent("text", text=block.text)
                        elif isinstance(block, ToolUseBlock):
                            yield AgentEvent(
                                "tool_use",
                                tool_name=block.name,
                                tool_input=block.input,
                            )
                        elif isinstance(block, ToolResultBlock):
                            content = block.content
                            text = content if isinstance(content, str) else str(content)
                            if len(text) > _TOOL_RESULT_CAP:
                                text = (
                                    text[:_TOOL_RESULT_CAP]
                                    + f"\n[TRUNCATED: showing {_TOOL_RESULT_CAP} of "
                                    f"{len(text)} chars — use Grep or offset to see more]"
                                )
                            yield AgentEvent("tool_result", text=text)
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
                    "api_error_status": None,
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
                )
        return final
