"""Structured escalation report (PLAN.md 22.4).

An escalation is *never* "I'm stuck." It is the six-part report a human can act
on in under a minute. We also parse the agent's structured blocker emission out
of its final text (a fenced ``BLOCKER_JSON`` block), failing safe to a
NOVEL_UNKNOWN escalation when nothing parseable is present.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .taxonomy import Blocker, BlockerCategory

# The agent is asked to emit its blocker report between these markers.
_BLOCKER_JSON = re.compile(
    r"BLOCKER_JSON_START\s*(.*?)\s*BLOCKER_JSON_END", re.DOTALL
)


def parse_blocker(text: str) -> Blocker | None:
    """Extract a Blocker from an agent's final text, or None if absent/malformed.

    Returning None lets the caller decide the fallback (the orchestrator turns it
    into a NOVEL_UNKNOWN escalation), so a missing block never silently passes.
    """
    if not text:
        return None
    match = _BLOCKER_JSON.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return Blocker.from_dict(data)


def render_report(blocker: Blocker, *, task_title: str, task_id: str) -> str:
    """Render the 22.4 six-part escalation report as markdown for the board /
    notification. Order and headings are fixed so a human scans it fast."""
    b = blocker
    lines = [
        f"# Blocker — {task_title}  ({task_id[:8]})",
        f"**Category:** {b.category.value}   "
        f"**Confidence:** {b.confidence:.0%}   "
        f"**Transient:** {'yes' if b.transient else 'no'}",
        "",
        "## 1. Goal",
        b.goal or "(not stated)",
        "",
        "## 2. What happened (evidence)",
        b.evidence.strip() or "(no evidence captured)",
        "",
        "## 3. Why blocked",
        b.root_cause_hypothesis or "(no hypothesis)",
        "",
        "## 4. What I tried",
    ]
    if b.tried:
        lines += [f"- {t}" for t in b.tried]
    else:
        lines.append("- (no alternatives attempted)")
    lines += ["", "## 5. What I need from you"]
    if b.question:
        lines.append(b.question)
        if b.options:
            lines += [f"  - [{i+1}] {opt}" for i, opt in enumerate(b.options)]
    else:
        lines.append("(no specific question — review the diagnosis above)")
    lines += [
        "",
        "## 6. State & resume",
        f"Branch: `{b.resume_branch or '(none)'}`  "
        f"Commit: `{(b.resume_commit or '')[:8] or '(none)'}` "
        f"[WIP-BLOCKED]",
    ]
    if b.wake_condition:
        lines.append(f"Wake condition: `{b.wake_condition}`")
    lines.append(f"Resume with: `nh reply {task_id[:8]} \"<answer>\"`")
    return "\n".join(lines)


def notification_line(blocker: Blocker, *, task_title: str, task_id: str) -> str:
    """One-line summary for Slack/email — the headline, not the full report."""
    ask = blocker.question or blocker.root_cause_hypothesis or "needs review"
    return (
        f"{task_title} [{task_id[:8]}] {blocker.category.value}: {ask}"
        f"  →  nh blocked  /  nh reply {task_id[:8]} \"...\""
    )


def fallback_blocker(detail: str, *, resume_branch: str = "",
                     resume_commit: str = "", goal: str = "") -> Blocker:
    """Construct a NOVEL_UNKNOWN blocker when the agent gave no structured report.

    Used for deterministic orchestrator-side failures (push failed, PR error,
    size limit) and for an agent that returned no parseable block."""
    return Blocker(
        category=BlockerCategory.NOVEL_UNKNOWN,
        transient=False,
        confidence=0.0,
        root_cause_hypothesis=detail,
        goal=goal,
        evidence=detail,
        question="Review the blocker and advise how to proceed.",
        resume_branch=resume_branch,
        resume_commit=resume_commit,
    )


def blocker_prompt_suffix() -> str:
    """Instruction appended to the agent prompt so it can self-report a blocker.

    The agent emits this ONLY when it genuinely cannot proceed without lowering
    the bar — never as a way to avoid finishing a doable task."""
    cats = ", ".join(c.value for c in BlockerCategory)
    return (
        "\n\nIf — and only if — you cannot make verifiable progress without "
        "weakening a test, expanding scope, editing the acceptance criteria, or "
        "faking completion, STOP and emit a structured blocker report. Do not "
        "lower the bar to force a pass. Emit it between markers exactly:\n"
        "BLOCKER_JSON_START\n"
        "{\n"
        f'  "category": one of [{cats}],\n'
        '  "transient": true|false,\n'
        '  "wake_condition": "machine-checkable, e.g. \'PR org/repo#12 merged\', '
        '\'quota_refreshed\', \'after:2h\', or null",\n'
        '  "root_cause_hypothesis": "...",\n'
        '  "confidence": 0.0-1.0,\n'
        '  "tried": ["alternative 1 + result", "alternative 2 + result"],\n'
        '  "question": "the ONE decision/info you need from a human, or null",\n'
        '  "options": ["candidate answer 1", "candidate answer 2"],\n'
        '  "goal": "the step you were attempting",\n'
        '  "evidence": "the exact command + output that shows the block"\n'
        "}\n"
        "BLOCKER_JSON_END\n"
        "Confidence below 0.6 means you are unsure what is wrong — that is fine, "
        "report it and a human will help; do not thrash."
    )
