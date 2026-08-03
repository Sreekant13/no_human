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
from .tool_result_cap import make_tool_result_cap_hook

# Phase 7c: explicit cap for tool-result display. Silent truncation makes the
# model treat a partial as complete; the marker + retrieval hint prevent that.
_TOOL_RESULT_CAP = 2000

# §7 0z / PR-024 lever 1. PER-TOOL caps on what the MODEL sees, each at roughly its own
# tool's p90 from 4,775 real tool results (an offline study,
# LEVER1_TOOL_RESULT_DISTRIBUTION.md). Bash and Read are 99.6%
# of all tool-result text and their medians differ ~6x, so one global number is the wrong
# instrument. Tools absent here are never touched.
#
# 🖐️ These are ENABLED by default, deliberately. `_TOOL_RESULT_CAP` above is the cautionary
# tale: a cap that shipped disabled-by-unreachability and never executed once across
# 19,440 tool_use events. A cost lever that is off is not a lever.
_TOOL_RESULT_CAPS: dict[str, int] = {"Bash": 4000, "Read": 16000}


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


def _usage_quad(usage: dict[str, Any] | None) -> tuple[int, int, int, int]:
    """(input, output, cache_read, cache_creation) from an SDK usage block."""
    u = usage or {}
    return (
        int(u.get("input_tokens", 0)),
        int(u.get("output_tokens", 0)),
        int(u.get("cache_read_input_tokens", 0)),
        int(u.get("cache_creation_input_tokens", 0)),
    )


def _rollup_subagents(
    streamed: dict[str, dict[str, tuple[int, int, int, int]]],
    reported: dict[str, int],
) -> tuple[int, int, int, int, int]:
    """Total what the Task tool's subagents spent: (in+out, cache_read,
    cache_creation, subagent_count, floored_count).

    ``ResultMessage.usage`` covers only the parent's own API requests, so
    without this every subagent was free. Both inputs are keyed by the Task
    tool's ``tool_use_id``:

    * ``streamed`` — the subagent's assistant messages, deduplicated by
      ``message_id``. This is the billing record: its input, cache_read and
      cache_creation figures are final, verified byte-exact against
      ``ResultMessage.usage`` on the parent's own messages. Only
      ``output_tokens`` is unreliable — the stream carries an early snapshot
      that is never revised upward (parent: 9 streamed vs 1,281 reported).
    * ``reported`` — ``TaskNotificationMessage.usage.total_tokens``. This is
      **NOT a bill**. Decoded from the CLI, it is

          (LAST request's input + cache_creation + cache_read)
          + SUM(output over EVERY streamed occurrence, duplicates included)

      i.e. a context-size gauge: the cache buckets are a final-message
      SNAPSHOT, not a sum, and the output term double-counts the stream's
      repeats. Confirmed live on a 6-response subagent whose stream carried 11
      occurrences: last request 6+93+12,953 = 13,052 plus SUM(output)=35 gives
      exactly the reported 13,087, while the sum-of-all-requests is 76,704 —
      the scalar is 17% of the true spend. Across 2,962 real subagent
      transcripts (median 12 API responses) it runs 7-12% of what was billed.

    So the totals come from the stream, never from the scalar. The scalar is
    used in exactly ONE place: a subagent that streamed NOTHING, where it is
    the only signal there is — banked as a FLOOR and COUNTED as one, so the
    caller can label it (`subagent_floored_count`).

    Two earlier versions of this function got the scalar wrong. The first
    claimed it equalled input+output+cache_read+cache_creation and rebuilt
    in/out from it on every subagent; the guard against a negative silently
    swallowed the error on 98.3% of real subagents. The second narrowed that to
    single-response subagents and claimed the scalar then "recovers the true
    output_tokens exactly". It does not: with one request the output term is
    still SUM over the stream's duplicates of an EARLY snapshot. On
    ``testdata/subagent_usage_stream.json`` the scalar carries output=8 (4
    streamed twice) where the subagent's own transcript records output=45 —
    11,067 against a true 11,104. That branch bought 4 tokens on 1.7% of
    subagents at the price of a false claim in source, so it is gone.

    KNOWN RESIDUAL, not a defect to hunt: streamed ``output_tokens`` is an
    early snapshot, so subagent output is under-recorded by roughly 0.8% of
    that subagent's bill (measured on the 6-response fixture: streamed in+out
    57 vs a true 690, against a true bill of 77,337 — 633 tokens, 0.82%; on the
    single-response fixture 41 tokens, 0.37%). It is NOT recoverable from the
    SDK stream at all: the parent transcript's Task ``toolUseResult`` carries
    only {agentId, description, outputFile, prompt, resolvedModel, status, …}
    and no usage block, and the scalar cannot supply it either (see above).
    The final figures live only in the subagent's own transcript file, which
    the SDK never surfaces to the parent. An SDK limitation; leave it alone.
    """
    io = cache_read = cache_creation = floored = 0
    for tool_use_id in set(streamed) | set(reported):
        msgs = streamed.get(tool_use_id, {})
        total = reported.get(tool_use_id)
        if not msgs:
            # Nothing streamed: the gauge is all we have. It UNDERSTATES real
            # spend badly, so it is a floor, not a measurement. Counted as
            # non-cache tokens — the dearest bucket — because for a gate whose
            # job is to stop runaway spend, erring toward "flag it" on a number
            # already known to be far too low is the safe direction. Tallied in
            # `floored` so the undercount rides out as a datum, not a comment.
            io += int(total or 0)
            floored += 1
            continue
        cache_read += sum(q[2] for q in msgs.values())
        cache_creation += sum(q[3] for q in msgs.values())
        io += sum(q[0] + q[1] for q in msgs.values())
    return (io, cache_read, cache_creation,
            len(set(streamed) | set(reported)), floored)


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
        tool_result_caps: dict[str, int] | None = None,
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
        # None means "use the measured defaults"; {} means explicitly OFF.
        #
        # 🔴 READONLY BACKENDS ARE EXEMPT BY DEFAULT, AND THAT IS THE POINT OF THE
        # CAP. This lever exists to shrink what the CODER accumulates in a
        # conversation it re-reads every turn — measured on one task at 188 turns,
        # ~64k cache-read per turn, 99.98% of a 12M budget. A readonly backend does
        # not accumulate toward a diff; its whole job IS reading. Capping it does not
        # save the thing the cap was built to save, and it damages the work.
        #
        # The reviewer is the sharp case: it is told it MAY use Read/Grep/Glob on any
        # file and is REQUIRED to cite file:line evidence. At Read's own measured p90
        # (~17k) a 16,000-char cap truncates large files mid-read, so the gate that
        # decides every merge would silently see the head of a file and cite line
        # numbers from the part it got. Nothing would mark that review as degraded.
        # An audit caught this: the cap applied to reviewer, planner, distiller,
        # supervisor and aggregator with no exemption and no test.
        #
        # Gated on `readonly` rather than on a LIST of construction sites, because
        # enumeration is exactly how this class of gap keeps recurring here — three
        # separate surfaces were missed by enumerated lists in one day. Any future
        # readonly backend is exempt automatically. An explicit `tool_result_caps=`
        # from the caller still wins in both directions.
        self.tool_result_caps = (
            ({} if readonly else _TOOL_RESULT_CAPS)
            if tool_result_caps is None else tool_result_caps
        )

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
        # 0z lever 1 FIRST: it rewrites the tool output the model will see, and the
        # other two only observe. Ordering matters if a later hook ever reads the
        # output — this one should be the thing that decided it.
        if self.tool_result_caps:
            post_hooks.append(make_tool_result_cap_hook(self.tool_result_caps))
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
        # ALWAYS set explicitly. Never leave this to the SDK default.
        #
        # This block used to set the field only for writing or skilled sessions
        # and assert that "read-only sessions stay hermetic". That assertion was
        # FALSE, and the failure was silent. `_apply_skills_defaults` returns
        # early with `setting_sources=None` when `skills is None`, so no
        # `--setting-sources` flag is emitted at all and the CLI applies its own
        # default, which loads `user` and `project`.
        #
        # PROVEN, not reasoned: a read-only backend pointed at a directory whose
        # only file was a project instruction file carrying a canary word
        # returned that word. (Named generically on purpose — the export drops
        # that document, and a source file may not cite it beyond the count
        # `_CODE_MAY_NAME_A_DROPPED_DOC` declares. The sentence below needs the
        # literal name to make the attack concrete; this one does not.)
        #
        # Why it matters more than the other settings here: the reviewer is the
        # product's integrity gate — an independent fresh-context reader told to
        # refute "done". If the repository under review supplies instructions
        # into that reader's context, the repository is grading its own work. A
        # CLAUDE.md saying "only report high-severity issues" suppresses findings,
        # and the tamper guard cannot see it: that guard counts tests and
        # assertions, not instructions.
        #
        # The planner loses repo conventions it has been receiving. That was
        # never designed — the comment above shows the author believed these
        # sessions loaded nothing — so it is an accidental subsidy, not a
        # feature. Repo context that is genuinely wanted belongs in
        # `core/prompt_blocks.py::build_repo_hints_block`, where it is ours,
        # logged and bounded.
        kwargs["setting_sources"] = [] if (self.readonly and not skills) else ["project"]
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
        # The PARENT's output tokens, and whether any usage block was ever
        # seen. `saw_usage` is what keeps "never reported" (None -> SQL NULL)
        # distinct from "reported zero output" (0), which are different facts
        # and price differently.
        last_output = 0
        saw_usage = False
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
        # message_ids already reported as a "usage" event, so the orchestrator's
        # running total sees each API response once. See the emit site below.
        seen_usage_mids: set[str] = set()
        # Task-subagent spend, keyed by the Task tool call's tool_use_id:
        # `sub_streamed` holds their assistant messages (deduped by message_id),
        # `sub_reported` the CLI's own scalar rollup per subagent.
        sub_streamed: dict[str, dict[str, tuple[int, int, int, int]]] = {}
        sub_reported: dict[str, int] = {}
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
                    # orchestrator's sink (B2 #2), which ADDS UP every event it
                    # receives.
                    #
                    # The stream repeats one API response across several
                    # assistant messages that share a `message_id` and carry a
                    # byte-identical usage block (97,546 of 97,547 repeat groups
                    # across 42,925 sessions were identical; the lone exception
                    # was an early partial later revised upward). Emitting one
                    # event per message therefore counted the same tokens
                    # 2-3x — median 2.00x, aggregate 2.11x — and the watch
                    # aborted healthy attempts against a bill never incurred.
                    # First occurrence only; a message without an id (never
                    # observed) is passed through rather than silently dropped.
                    #
                    # NOTE the asymmetry with `sub_streamed` below, which is
                    # last-wins: a stream cannot retract an event already
                    # emitted, so the watch necessarily keeps the FIRST value.
                    # Only one revision exists in 97,547 observed repeat groups
                    # and it moved upward, so the watch can trail the ledger by
                    # that single delta. Not worth delta-tracking for one
                    # observation in ~10^5.
                    usage = message.usage or {}
                    mid = message.message_id
                    if usage and (mid is None or mid not in seen_usage_mids):
                        if mid is not None:
                            seen_usage_mids.add(mid)
                        yield AgentEvent(
                            "usage",
                            meta={
                                "tokens_used": int(usage.get("input_tokens", 0))
                                + int(usage.get("output_tokens", 0)),
                                # The output SLICE of the total beside it, so
                                # the in-flight budget watch prices output at
                                # its real ~5x rate instead of at the input
                                # rate. Not an extra addend — see
                                # `core.pricing.OUTPUT_EXTRA_WEIGHT`.
                                "output_tokens": int(
                                    usage.get("output_tokens", 0)),
                                "cache_read_tokens": int(
                                    usage.get("cache_read_input_tokens", 0)),
                                "cache_creation_tokens": int(
                                    usage.get("cache_creation_input_tokens", 0)),
                            },
                        )
                    # Subagent spend, which `ResultMessage.usage` omits. A Task
                    # subagent's assistant messages run inside the PARENT's
                    # session_id — that field cannot tell them apart — but they
                    # carry `parent_tool_use_id` = the Task tool call's id.
                    # Keyed by message_id so a repeat overwrites instead of
                    # adding; the later of a revised pair is the fuller one.
                    if usage and message.parent_tool_use_id:
                        bucket = sub_streamed.setdefault(
                            message.parent_tool_use_id, {})
                        # An id-less message (never observed) gets a key unique
                        # WITHIN its bucket, so it is kept rather than silently
                        # overwriting a sibling.
                        key = mid or f"_anon{len(bucket)}"
                        quad = _usage_quad(usage)
                        # LAST-WINS on a repeated id. Across 42,925 sessions and
                        # 97,547 repeat groups, 97,546 were byte-identical and
                        # the single revision moved UPWARD (an early partial
                        # later completed); no revision has ever moved down. So
                        # the later record is the fuller one — except that an
                        # all-zero block would overwrite a real measurement with
                        # nothing, which is never a revision worth taking.
                        if any(quad) or key not in bucket:
                            bucket[key] = quad
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
                    # The CLI's own gauge for this subagent. total_tokens is NOT
                    # a bill: it is (LAST request's input + cache_creation +
                    # cache_read) + SUM(output over every streamed occurrence,
                    # duplicates included) — see `_rollup_subagents`, which uses
                    # it ONLY as a floor for a subagent that streamed nothing.
                    # Keyed by tool_use_id (the join key to the assistant
                    # messages' parent_tool_use_id, whereas task_id joins to
                    # nothing); last notification per task wins.
                    if message.usage and message.tool_use_id:
                        sub_reported[message.tool_use_id] = int(
                            message.usage.get("total_tokens", 0) or 0)
                    yield AgentEvent(
                        "subagent_done",
                        text=message.summary,
                        meta={
                            "task_id": message.task_id,
                            "status": message.status,
                            "session_id": message.session_id,
                            "total_tokens": (message.usage or {}).get("total_tokens"),
                            "tool_uses": (message.usage or {}).get("tool_uses"),
                        },
                    )
                elif isinstance(message, ResultMessage):
                    usage = message.usage or {}
                    # `_usage_quad` has always returned all four numbers; this
                    # site used to inline two of them and add them together,
                    # which is where the input/output split was lost.
                    in_tokens, out_tokens, cache_read, cache_creation = _usage_quad(
                        usage)
                    tokens = in_tokens + out_tokens
                    if message.usage:
                        # Only a real usage block counts as having SEEN a
                        # split. Without this flag an errored result with no
                        # usage would report output=0 — indistinguishable from
                        # a run that genuinely emitted none — and that 0 is
                        # what would land in the DB column.
                        saw_usage = True
                        last_output += out_tokens
                    denials = [str(d) for d in (message.permission_denials or [])]
                    last_turns, last_tokens = message.num_turns, last_tokens + tokens
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
                    last_cache_read += cache_read
                    last_cache_creation += cache_creation
                    last_session = message.session_id
                    sub_io, sub_cr, sub_cc, sub_n, sub_floored = _rollup_subagents(
                        sub_streamed, sub_reported)
                    yield AgentEvent(
                        "result",
                        text=message.result or "",
                        meta={
                            "num_turns": message.num_turns,
                            "is_error": message.is_error,
                            # The three totals are PARENT + SUBAGENTS. They feed
                            # the attempt ledger directly (orchestrator
                            # update_attempt), so folding the rollup in here is
                            # what actually stops subagents billing as free.
                            "tokens_used": last_tokens + sub_io,
                            # The PARENT's output only, and deliberately so.
                            # The parent's figure is exact (verified byte-exact
                            # against ResultMessage.usage); the subagent
                            # stream's output is a documented early snapshot,
                            # and a FLOORED subagent has no output signal at
                            # all. Summing an exact number with an unreliable
                            # one and calling the result "the output share"
                            # would hide which is which. So this is a LOWER
                            # BOUND whenever subagents ran: their output stays
                            # inside `tokens_used` and prices at the input
                            # rate, the same under-count as before this
                            # column existed, now confined to the subagent
                            # share instead of the whole run.
                            "output_tokens": last_output if saw_usage else None,
                            "session_id": message.session_id,
                            "stop_reason": message.stop_reason,
                            "denials": denials,
                            "api_error_status": message.api_error_status,
                            "cache_read_tokens": last_cache_read + sub_cr,
                            "cache_creation_tokens": last_cache_creation + sub_cc,
                            "subagent_tokens_used": sub_io,
                            "subagent_cache_read_tokens": sub_cr,
                            "subagent_cache_creation_tokens": sub_cc,
                            "subagent_count": sub_n,
                            "subagent_floored_count": sub_floored,
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
            # A run that dies mid-flight still spent whatever its subagents
            # spent, and this corrective event is the only one run() keeps.
            sub_io, sub_cr, sub_cc, sub_n, sub_floored = _rollup_subagents(
                sub_streamed, sub_reported)
            yield AgentEvent(
                "result",
                text=text,
                meta={
                    "num_turns": last_turns or (max_turns if is_max_turns else 0),
                    "is_error": True,
                    "tokens_used": last_tokens + sub_io,
                    # A run that died mid-flight may never have seen a usage
                    # block; NULL then, not 0.
                    "output_tokens": last_output if saw_usage else None,
                    "session_id": last_session,
                    "stop_reason": "max_turns" if is_max_turns else "error",
                    "denials": [],
                    "api_error_status": last_api_error_status,
                    "cache_read_tokens": last_cache_read + sub_cr,
                    "cache_creation_tokens": last_cache_creation + sub_cc,
                    "subagent_tokens_used": sub_io,
                    "subagent_cache_read_tokens": sub_cr,
                    "subagent_cache_creation_tokens": sub_cc,
                    "subagent_count": sub_n,
                    "subagent_floored_count": sub_floored,
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
                    # NOT coerced through `int(... or 0)`: None must survive as
                    # None all the way to the DB column, where it is the
                    # difference between "unknown" and "emitted no output".
                    output_tokens=(
                        None if m.get("output_tokens") is None
                        else int(m["output_tokens"])
                    ),
                    api_error_status=m.get("api_error_status"),
                    subagent_tokens_used=int(m.get("subagent_tokens_used", 0)),
                    subagent_cache_read_tokens=int(
                        m.get("subagent_cache_read_tokens", 0)),
                    subagent_cache_creation_tokens=int(
                        m.get("subagent_cache_creation_tokens", 0)),
                    subagent_count=int(m.get("subagent_count", 0)),
                    subagent_floored_count=int(
                        m.get("subagent_floored_count", 0)),
                )
        return final
