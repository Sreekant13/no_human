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
    AssistantMessage,
    ClaudeAgentOptions,
    HookContext,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    query,
)

from . import guard


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


def _make_guard_hook(
    forbidden_paths: list[str], never_push_to: list[str]
) -> Callable[..., Awaitable[dict]]:
    """Build a PreToolUse hook callback that applies the pure guard policy."""

    async def hook(input_data: dict, tool_use_id: str | None, context: HookContext):
        decision = guard.evaluate(
            input_data.get("tool_name", ""),
            input_data.get("tool_input", {}) or {},
            forbidden_paths=forbidden_paths,
            never_push_to=never_push_to,
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


class ClaudeBackend:
    """Drives one Agent SDK session per call to :meth:`run`."""

    def __init__(
        self,
        *,
        model: str,
        forbidden_paths: list[str] | None = None,
        never_push_to: list[str] | None = None,
        permission_mode: str = "bypassPermissions",
    ):
        self.model = model
        self.forbidden_paths = forbidden_paths or [".env", "secrets/", "*.key", "*.pem"]
        self.never_push_to = never_push_to or ["main", "master", "release/*"]
        # bypassPermissions: unattended autonomy. The PreToolUse guard is the
        # real safety boundary and fires even in this mode (Part 10).
        self.permission_mode = permission_mode

    def _options(
        self, cwd: Path, max_turns: int, *, effort: str | None = None,
        resume: str | None = None,
    ) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            model=self.model,
            cwd=str(cwd),
            max_turns=max_turns,
            permission_mode=self.permission_mode,
            effort=effort,
            resume=resume,
            hooks={
                "PreToolUse": [
                    HookMatcher(
                        matcher=None,
                        hooks=[
                            _make_guard_hook(self.forbidden_paths, self.never_push_to)
                        ],
                    )
                ]
            },
        )

    async def stream(
        self,
        prompt: str,
        *,
        cwd: Path,
        max_turns: int,
        effort: str | None = None,
        resume: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run the agent, yielding normalized events; the final event is ``result``."""
        options = self._options(cwd, max_turns, effort=effort, resume=resume)
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
                        yield AgentEvent("tool_result", text=text[:2000])
            elif isinstance(message, ResultMessage):
                usage = message.usage or {}
                tokens = int(usage.get("input_tokens", 0)) + int(
                    usage.get("output_tokens", 0)
                )
                denials = [str(d) for d in (message.permission_denials or [])]
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
    ) -> AgentResult:
        """Run to completion, optionally forwarding each event, return the result."""
        final = AgentResult(
            final_text="", num_turns=0, is_error=False, tokens_used=0,
            session_id=None, stop_reason=None,
        )
        async for event in self.stream(
            prompt, cwd=cwd, max_turns=max_turns, effort=effort, resume=resume
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
                )
        return final
