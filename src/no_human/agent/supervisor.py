"""Supervisor hook — a PostToolUse evaluator that course-corrects the working
agent in real time (SUPERVISOR_AND_REVIEWER_PLAN.md Phase A).

Replaces the human-in-the-loop for:
  - Rule enforcement (the agent ignored a confirmed rule)
  - Progress tracking (the agent is drifting off-task)
  - Question answering (the agent needs domain info available in the context)
  - Doom detection (the agent is stuck and the attempt should abort early)

The hook fires every ``check_every`` tool calls (default 5). Between firings it
accumulates tool-call summaries in a bounded sliding window. On each firing it
runs a fast, focused LLM evaluation that returns one of CONTINUE / CORRECT /
ANSWER / STOP.

The hook output uses the SDK's ``additionalContext`` field to inject corrections
visible to the working agent, or ``continue_: False`` to abort.

This module is pure policy + one LLM call. It has no I/O beyond that call, so
it is fully testable with a fake backend.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

log = logging.getLogger("no_human.supervisor")

# Output tags the LLM must emit exactly one of.
_TAG_CONTINUE = "SUPERVISOR_CONTINUE"
_TAG_CORRECT = "SUPERVISOR_CORRECT"
_TAG_ANSWER = "SUPERVISOR_ANSWER"
_TAG_STOP = "SUPERVISOR_STOP"

# Limit how much tool response text we keep per call (controls prompt size).
_RESPONSE_CAP = 1500
_WINDOW_SIZE = 10  # max tool calls in the sliding window
_TEXT_BUFFER = 6   # recent assistant utterances kept for the assumption/skill checks

# Phrases that signal the agent is asserting it CAN'T do something — the headline
# failure ("I can't access the PR" when a skill/access exists). Deterministic,
# cheap, and runs before any LLM call (EVOLUTION_PLAN §1.2: per-call interception
# uses a cheap deterministic check, not an LLM on every call).
_INABILITY = re.compile(
    r"\b(i\s+can'?t|i\s+cannot|i'?m\s+unable|i\s+am\s+unable|there'?s\s+no\s+way|"
    r"i\s+do\s*n'?t\s+have\s+access|cannot\s+access|can'?t\s+access|"
    r"no\s+access\s+to|not\s+able\s+to\s+access|unable\s+to\s+access)\b",
    re.IGNORECASE,
)


@dataclass
class SupervisorDecision:
    """The parsed outcome of one supervisor evaluation."""

    action: str  # "continue", "correct", "answer", "stop"
    message: str = ""  # correction/answer/stop-reason text (empty for continue)
    raw: str = ""  # full LLM output for logging


@dataclass
class ToolCallRecord:
    """One tool call in the sliding window."""

    tool_name: str
    tool_input_summary: str  # truncated for prompt brevity
    tool_response_summary: str  # truncated


def _summarise_input(tool_input: dict[str, Any]) -> str:
    """Compact summary of a tool input dict (max 300 chars)."""
    if not tool_input:
        return "(empty)"
    # For Bash, show the command. For Edit/Write, show the path.
    cmd = tool_input.get("command", "")
    if cmd:
        return cmd[:300]
    path = (
        tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("notebook_path")
        or ""
    )
    if path:
        return f"path={path}"[:300]
    # Fallback: first key=value pairs.
    parts = [f"{k}={str(v)[:60]}" for k, v in list(tool_input.items())[:4]]
    return ", ".join(parts)[:300]


def _summarise_response(tool_response: Any) -> str:
    """Compact summary of a tool response (max _RESPONSE_CAP chars)."""
    text = str(tool_response) if tool_response else "(empty)"
    if len(text) > _RESPONSE_CAP:
        return text[:_RESPONSE_CAP] + "…"
    return text


def parse_decision(text: str) -> SupervisorDecision:
    """Parse the LLM's structured output into a SupervisorDecision.

    The LLM must emit exactly one of the four tags. Text after the tag (up to
    the next tag or end) is the message.
    """
    text = (text or "").strip()
    for tag, action in [
        (_TAG_STOP, "stop"),
        (_TAG_CORRECT, "correct"),
        (_TAG_ANSWER, "answer"),
        (_TAG_CONTINUE, "continue"),
    ]:
        idx = text.find(tag)
        if idx != -1:
            after = text[idx + len(tag) :].strip()
            # Take everything after the tag until EOF or another tag.
            for other_tag in [_TAG_CONTINUE, _TAG_CORRECT, _TAG_ANSWER, _TAG_STOP]:
                if other_tag != tag:
                    end = after.find(other_tag)
                    if end != -1:
                        after = after[:end].strip()
            return SupervisorDecision(action=action, message=after, raw=text)
    # No recognisable tag → default to CONTINUE (safe fallback: don't
    # inject noise when the LLM's response is unparseable).
    log.warning("supervisor: unparseable LLM output, defaulting to CONTINUE")
    return SupervisorDecision(action="continue", raw=text)


def detect_inability(text: str, skills: list[str] | None) -> SupervisorDecision | None:
    """Deterministic "I can't / skill-exists" detector (EVOLUTION_PLAN §1.2 #2,
    decision-table row 1). The headline failure: the agent claims it cannot do
    something ("I can't access the PR") instead of checking the skills/access it
    has. Returns a CORRECT decision when an inability claim is present, naming
    the available skills; ``None`` otherwise. Cheap, no LLM — runs every check.
    """
    if not text or not _INABILITY.search(text):
        return None
    # Quote the offending snippet so the correction is concrete.
    m = _INABILITY.search(text)
    start = max(0, m.start() - 10)
    snippet = text[start:m.end() + 60].strip().replace("\n", " ")
    skill_names = [s for s in (skills or []) if s]
    if skill_names:
        skills_hint = (
            "Available skills/memories you have NOT checked: "
            + ", ".join(skill_names[:8])
            + ". Check whether one of these (or configured access, e.g. GHE/PR "
            "auth) makes this possible."
        )
    else:
        skills_hint = (
            "Do not conclude you can't until you have ACTUALLY tried and shown the "
            "failing output — configured access (e.g. GHE/PR auth) may already exist."
        )
    return SupervisorDecision(
        action="correct",
        message=(
            f"You asserted inability (\"{snippet}…\"). Do NOT claim you can't "
            f"before verifying. {skills_hint}"
        ),
        raw="[deterministic skill-exists detector]",
    )


def build_evaluation_prompt(
    *,
    task_title: str,
    acceptance_criteria: list[str],
    rules: str,
    profile_context: str,
    window: list[ToolCallRecord],
    total_calls: int,
    skills: str = "",
    recent_text: str = "",
    declared_files: str = "",
) -> str:
    """Build the prompt sent to the supervisor LLM."""
    criteria = "\n".join(f"  - {c}" for c in acceptance_criteria) or "  (none)"
    calls_block = "\n".join(
        f"  [{i+1}] {r.tool_name}: {r.tool_input_summary}\n"
        f"       → {r.tool_response_summary}"
        for i, r in enumerate(window)
    )
    skills_block = (
        f"Skills available to the agent (it must USE these, not reinvent or claim "
        f"they don't exist):\n{skills}\n\n" if skills else ""
    )
    said_block = (
        f"What the agent recently SAID (watch for unverified claims / 'I can't'):\n"
        f"{recent_text}\n\n" if recent_text else ""
    )
    # P5: scope drift. Only present when the plan declared a file set — matches
    # the deterministic scope guard's advisory-when-empty behaviour. Framed
    # conservatively so legitimate refactors are not falsely corrected.
    scope_block = (
        "SCOPE — the plan declared these files to change/create:\n"
        f"{declared_files}\n"
        "The agent MAY edit files outside this set when justified (a necessary "
        "refactor, or a file the plan missed). Only CORRECT if there is a PATTERN "
        "of edits outside this set with NO stated justification — then list the "
        "declared files and tell it to justify the out-of-scope edits or revert "
        "them.\n\n"
        if declared_files else ""
    )
    return (
        "You are the Supervisor of an autonomous coding agent. You stand in for a "
        "senior engineer watching over the agent's shoulder. Your ONLY job is to "
        "keep it on-task, enforce the rules, and catch mistakes early.\n\n"
        f"Task: {task_title}\n"
        f"Acceptance criteria:\n{criteria}\n\n"
        f"{profile_context}\n"
        f"{skills_block}"
        f"Rules the agent must follow:\n{rules}\n\n"
        f"Recent tool calls ({len(window)} of {total_calls} total):\n{calls_block}\n\n"
        f"{said_block}"
        f"{scope_block}"
        "Evaluate the agent's recent actions. Respond with EXACTLY ONE block:\n\n"
        f"{_TAG_CONTINUE}\n"
        "(Agent is on track. No correction needed.)\n\n"
        f"{_TAG_CORRECT}\n"
        "<specific correction: what went wrong and what to do instead>\n\n"
        f"{_TAG_ANSWER}\n"
        "<answer to a question the agent asked or implied, using the rules/context>\n\n"
        f"{_TAG_STOP}\n"
        "<reason this attempt is doomed and should be aborted>\n\n"
        "What to watch for (EVOLUTION_PLAN §1.3 — cite evidence, never a number):\n"
        "  - SKILL-EXISTS: the agent claims it 'can't' access/do something while a "
        "skill or configured access exists → CORRECT, naming the skill and how to use it.\n"
        "  - UNVERIFIED ASSUMPTION: the agent states system state/behaviour with no "
        "cited evidence (no command output, no file read) → CORRECT, demand it verify first.\n"
        "  - KNOWN GAP: the agent declares a step done while a gap is nameable → "
        "CORRECT, name the gap and require it be closed (never accept a self-score).\n"
        "  - RULE VIOLATION: the agent breaks a confirmed rule → CORRECT, cite the rule.\n"
        "  - DOOM LOOP: same hypothesis tried 3× / wrong repo → STOP.\n\n"
        "Rules for your evaluation:\n"
        "  - Only CORRECT when you have concrete evidence of drift or rule violation.\n"
        "  - Only ANSWER when the agent clearly needs info you can provide.\n"
        "  - Only STOP when the attempt is provably doomed (e.g., stuck loop, wrong repo).\n"
        "  - When in doubt, CONTINUE. A false correction is worse than no correction.\n"
        "  - Keep corrections SHORT (1-3 sentences). Do not restate the task.\n"
    )


def build_preflight_prompt(
    *,
    task_title: str,
    acceptance_criteria: list[str],
    rules: str,
    skills: str,
    plan: str,
) -> str:
    """Pre-flight plan check (EVOLUTION_PLAN §1.2 #1): one evaluation before the
    first edit. Does the plan cover every acceptance criterion? Does it violate a
    confirmed rule? What is the devil's-advocate failure mode? Reuses the same
    CONTINUE/CORRECT contract so the orchestrator can inject a correction."""
    criteria = "\n".join(f"  - {c}" for c in acceptance_criteria) or "  (none)"
    skills_block = f"Skills available:\n{skills}\n\n" if skills else ""
    return (
        "You are the Supervisor reviewing an autonomous coding agent's PLAN before "
        "it writes any code. Catch gaps now, when they are cheap to fix.\n\n"
        f"Task: {task_title}\n"
        f"Acceptance criteria:\n{criteria}\n\n"
        f"{skills_block}"
        f"Confirmed rules:\n{rules}\n\n"
        f"The agent's proposed plan:\n{plan}\n\n"
        "Check, citing evidence (never a numeric score):\n"
        "  1. Does the plan address EVERY acceptance criterion? Name any it misses.\n"
        "  2. Does it violate any confirmed rule? Cite the rule.\n"
        "  3. Devil's advocate: what is the most likely way this plan fails? Is it "
        "handled?\n"
        "  4. Does it claim something is impossible that a listed skill enables?\n\n"
        "Respond with EXACTLY ONE block:\n\n"
        f"{_TAG_CONTINUE}\n(The plan is sound — covers the criteria, breaks no rule.)\n\n"
        f"{_TAG_CORRECT}\n<the specific gaps to fix before coding>\n"
    )


# Type alias for the LLM call function injected into SupervisorHook.
# Signature: (prompt: str) -> str (returns the LLM's text response).
LLMCall = Callable[[str], Awaitable[str]]


class SupervisorHook:
    """PostToolUse hook that periodically evaluates the working agent."""

    def __init__(
        self,
        *,
        task_title: str,
        acceptance_criteria: list[str],
        rules: str,
        profile_context: str = "",
        skills: list[str] | None = None,
        llm_call: LLMCall,
        check_every: int = 5,
        window_size: int = _WINDOW_SIZE,
        on_decision: Callable[[SupervisorDecision], None] | None = None,
        declared_files: list[str] | None = None,
        budget_status: Callable[[], tuple[int, int] | None] | None = None,
    ):
        self.task_title = task_title
        self.acceptance_criteria = acceptance_criteria
        self.rules = rules
        self.profile_context = profile_context
        self.skills = skills or []
        # P5: the plan's declared FILES TO CHANGE/CREATE set, so the supervisor
        # can catch out-of-scope drift. Empty → scope check is a no-op (advisory).
        self.declared_files = declared_files or []
        self._llm_call = llm_call
        self.check_every = max(1, check_every)
        self._window: deque[ToolCallRecord] = deque(maxlen=window_size)
        self._recent_text: deque[str] = deque(maxlen=_TEXT_BUFFER)
        self._call_count = 0
        self._on_decision = on_decision
        # v8 budget nudge: (spent, ceiling) for the RUNNING attempt, or None.
        # Deterministic and LLM-free; checked on every PostToolUse so the
        # wrap-up correction lands BEFORE the sink's hard BudgetAbort.
        self.budget_status = budget_status
        self._budget_warned = False

    def record(self, tool_name: str, tool_input: dict, tool_response: Any) -> None:
        """Record a completed tool call in the sliding window."""
        self._window.append(
            ToolCallRecord(
                tool_name=tool_name,
                tool_input_summary=_summarise_input(tool_input),
                tool_response_summary=_summarise_response(tool_response),
            )
        )
        self._call_count += 1

    def note_text(self, text: str) -> None:
        """Record an assistant utterance so the supervisor can see what the agent
        SAYS (not just the tools it runs) — the place 'I can't' claims and
        unverified assumptions surface. Best-effort, bounded buffer."""
        if text and text.strip():
            self._recent_text.append(text.strip())

    def _skills_text(self) -> str:
        return "\n".join(f"  - {s}" for s in self.skills) if self.skills else ""

    def _declared_files_text(self) -> str:
        # Bound the list so a large plan can't bloat the every-N prompt.
        return (
            "\n".join(f"  - {f}" for f in self.declared_files[:20])
            if self.declared_files else ""
        )

    @property
    def should_evaluate(self) -> bool:
        """True when it's time to run the LLM evaluation."""
        return self._call_count > 0 and self._call_count % self.check_every == 0

    async def evaluate(self) -> SupervisorDecision:
        """Run the supervisor evaluation on the current window.

        First a cheap, deterministic skill-exists check on what the agent recently
        said (no LLM). If the agent asserted inability, CORRECT immediately. Only
        when that is clean do we spend an LLM call on the broader evaluation."""
        recent_text = "\n".join(self._recent_text)
        det = detect_inability(recent_text, self.skills)
        if det is not None:
            if self._on_decision:
                self._on_decision(det)
            return det

        prompt = build_evaluation_prompt(
            task_title=self.task_title,
            acceptance_criteria=self.acceptance_criteria,
            rules=self.rules,
            profile_context=self.profile_context,
            window=list(self._window),
            total_calls=self._call_count,
            skills=self._skills_text(),
            recent_text=recent_text,
            declared_files=self._declared_files_text(),
        )
        try:
            raw = await self._llm_call(prompt)
        except Exception as exc:  # noqa: BLE001
            log.warning("supervisor LLM call failed: %s", exc)
            return SupervisorDecision(action="continue", raw=f"LLM error: {exc}")
        decision = parse_decision(raw)
        if self._on_decision:
            self._on_decision(decision)
        return decision

    async def preflight(self, plan: str) -> SupervisorDecision:
        """Pre-flight plan check (EVOLUTION_PLAN §1.2 #1): evaluate the agent's
        plan before the first edit. Returns CONTINUE if sound, else CORRECT with
        the gaps to fix. LLM failure fails open (CONTINUE) — never block on the
        supervisor's own error."""
        prompt = build_preflight_prompt(
            task_title=self.task_title,
            acceptance_criteria=self.acceptance_criteria,
            rules=self.rules,
            skills=self._skills_text(),
            plan=plan,
        )
        try:
            raw = await self._llm_call(prompt)
        except Exception as exc:  # noqa: BLE001
            log.warning("supervisor preflight LLM call failed: %s", exc)
            return SupervisorDecision(action="continue", raw=f"LLM error: {exc}")
        decision = parse_decision(raw)
        if self._on_decision:
            self._on_decision(decision)
        return decision

    async def hook(
        self, input_data: dict, tool_use_id: str | None, context: Any
    ) -> dict:
        """The SDK PostToolUse hook callback.

        Called by the SDK after every tool execution. Records the call, and
        every ``check_every`` calls, runs the LLM evaluation.

        Returns a dict that the SDK interprets:
          - Empty dict → no action
          - ``hookSpecificOutput.additionalContext`` → inject correction
          - ``continue_: False`` → abort the session
        """
        self.record(
            tool_name=input_data.get("tool_name", ""),
            tool_input=input_data.get("tool_input", {}),
            tool_response=input_data.get("tool_response"),
        )

        # Budget nudge (research §8: preemptive forced generation): at 85% of
        # the armed attempt ceiling, ONE deterministic correction telling the
        # agent to write its deliverable NOW — the ns-0e7bf1ae class died with
        # the answer unwritten because only the 100% hard abort existed. Runs
        # every call (not on the LLM cadence) and never blocks on its own error.
        if self.budget_status is not None and not self._budget_warned:
            try:
                status = self.budget_status()
            except Exception:  # noqa: BLE001 — advisory, never break the hook
                status = None
            if status is not None and status[1] > 0:
                spent, ceiling = status
                if spent >= 0.85 * ceiling:
                    self._budget_warned = True
                    message = (
                        f"[SUPERVISOR] BUDGET: you have spent "
                        f"{spent:,} of this attempt's {ceiling:,}-token "
                        "budget; the attempt is force-stopped at 100%. "
                        "STOP exploring NOW and produce your final "
                        "deliverable immediately with the evidence you "
                        "already have. Any criterion you cannot cite "
                        "evidence for is NOT-MET — report it honestly. "
                        "Do NOT fabricate results or edits to look "
                        "finished."
                    )
                    # Observable firing: on_decision → supervisor_decision
                    # event → task_events. Whether this nudge fired must be
                    # drillable post hoc (the v9 budget-class blocker).
                    # Guarded: a raising sink must never cost the coder the
                    # wrap-up injection — the latch above already committed us.
                    if self._on_decision:
                        try:
                            self._on_decision(SupervisorDecision(
                                action="budget_nudge", message=message))
                        except Exception:  # noqa: BLE001 — advisory, never break the hook
                            log.warning("budget-nudge on_decision sink raised; "
                                        "injection still delivered")
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PostToolUse",
                            "additionalContext": message,
                        }
                    }

        if not self.should_evaluate:
            return {}

        decision = await self.evaluate()
        log.info(
            "supervisor decision: %s (call #%d)", decision.action, self._call_count
        )

        if decision.action == "continue":
            return {}

        if decision.action in ("correct", "answer"):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        f"[SUPERVISOR] {decision.message}"
                    ),
                }
            }

        if decision.action == "stop":
            return {
                "continue_": False,
                "stopReason": f"Supervisor abort: {decision.message}",
            }

        return {}  # unknown action → safe fallback
