"""B2: Native intake interrogation ("grill").

A relentless one-question-at-a-time clarifier that front-loads human input
to produce a crisp, unambiguous spec + acceptance criteria before the agent
starts implementing.

Pattern adapted from mattpocock's grilling skill:
  - One question at a time with suggested answers
  - Walk the decision tree
  - Substitute codebase exploration for questions the code can answer
  - Never ask what the code already shows

Runs at the one moment the human is present (task creation), then they step
out and the autonomous pipeline takes over.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class GrillQuestion:
    """One clarifying question from the intake grill."""

    question: str
    suggestions: list[str]
    round: int


@dataclass
class GrillResult:
    """The final refined spec produced by the grill."""

    title: str
    description: str
    acceptance_criteria: list[str]
    qa_log: list[dict] = field(default_factory=list)


MAX_ROUNDS_DEFAULT = 5


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


_GRILL_PROMPT = """\
You are a relentless intake interviewer for an autonomous coding agent.
Your job: take a potentially vague task and turn it into a crisp,
unambiguous specification with testable acceptance criteria — so the
implementing agent can work WITHOUT any further human interaction.

RULES:
1. Explore the codebase FIRST (use Read, Grep, Glob tools) to understand
   the existing code, patterns, and conventions. NEVER ask the user
   something the code already answers.
2. Ask ONE question at a time. Provide 2-4 suggested answers (labeled
   A, B, C, D) based on what you found in the code.
3. Each question must resolve a genuine ambiguity about INTENT, SCOPE,
   or CONSTRAINTS — things only the human can decide.
4. When you have enough clarity to write unambiguous, testable acceptance
   criteria, STOP asking and emit the final spec.
5. You must emit the final spec after at most {max_rounds} questions total.
{force_clause}
You are on round {round_n} of {max_rounds}.

TASK:
Title: {title}
Description: {description}
{qa_section}
Respond with a fenced ```json block in EXACTLY one of these two formats:

To ask a question:
```json
{{"type": "question", "question": "your single question", "suggestions": ["A: first option", "B: second option"]}}
```

To emit the final spec (when you have enough clarity):
```json
{{"type": "done", "title": "refined title", "description": "clear description of what to implement", "acceptance_criteria": ["criterion 1", "criterion 2"]}}
```

Emit EXACTLY one ```json block. No other prose after it.
"""


def _build_qa_section(qa_history: list[dict]) -> str:
    if not qa_history:
        return ""
    lines = ["Previous Q&A:"]
    for i, qa in enumerate(qa_history, 1):
        lines.append(f"  Q{i}: {qa['question']}")
        lines.append(f"  A{i}: {qa['answer']}")
    return "\n".join(lines) + "\n"


def parse_grill_response(
    text: str, round_n: int, qa_history: list[dict],
) -> GrillQuestion | GrillResult:
    """Parse the agent's response into a question or final result."""
    blocks = _JSON_BLOCK.findall(text or "")
    data = None
    if blocks:
        try:
            data = json.loads(blocks[-1])
        except json.JSONDecodeError:
            pass
    if data is None:
        # Try the whole text as JSON
        try:
            data = json.loads((text or "").strip())
        except (json.JSONDecodeError, TypeError):
            pass
    if data is None:
        return GrillQuestion(
            question="Could you describe what you want in more detail?",
            suggestions=["A: Let me add more context", "B: Proceed with what we have"],
            round=round_n,
        )

    if data.get("type") == "done":
        return GrillResult(
            title=data.get("title", ""),
            description=data.get("description", ""),
            acceptance_criteria=data.get("acceptance_criteria", []),
            qa_log=list(qa_history),
        )

    return GrillQuestion(
        question=data.get("question", "Please clarify your requirements."),
        suggestions=data.get("suggestions", []),
        round=round_n,
    )


async def grill_step(
    title: str,
    description: str | None,
    repo_path: str | None,
    qa_history: list[dict],
    backend: Any,
    *,
    max_rounds: int = MAX_ROUNDS_DEFAULT,
) -> GrillQuestion | GrillResult:
    """Run one step of the intake grill.

    Each call is stateless — the caller maintains the Q&A history. Returns a
    :class:`GrillQuestion` if more clarification is needed, or a
    :class:`GrillResult` when the spec is clear enough.

    The backend should be a read-only :class:`ClaudeBackend` instance so the
    agent can explore the codebase (Read/Grep/Glob) but cannot modify it.
    """
    round_n = len(qa_history) + 1
    force = round_n >= max_rounds

    force_clause = (
        "\nThis is the LAST round. You MUST emit the final spec now "
        '(type: "done"). Do NOT ask another question.\n'
        if force
        else ""
    )

    prompt = _GRILL_PROMPT.format(
        title=title,
        description=description or "(none provided)",
        qa_section=_build_qa_section(qa_history),
        round_n=round_n,
        max_rounds=max_rounds,
        force_clause=force_clause,
    )

    cwd = Path(repo_path) if repo_path else Path.home()
    result = await backend.run(
        prompt,
        cwd=cwd,
        max_turns=10,
        effort="low",
    )

    parsed = parse_grill_response(result.final_text, round_n, qa_history)

    # Past max rounds and still got a question — force a result with what
    # we have so the pipeline is never blocked indefinitely.
    if force and isinstance(parsed, GrillQuestion):
        return GrillResult(
            title=title,
            description=description or "",
            acceptance_criteria=[f"Implement: {title}"],
            qa_log=list(qa_history),
        )

    return parsed
