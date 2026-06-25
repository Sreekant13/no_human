"""Independent adversarial reviewer (PLAN.md Part 4.4, §3.3).

A fresh-context Agent SDK session told to *find faults and refute "done."*
Runs as ``claude-sonnet-4-6`` (different model from the implementer) with a
read-only guard so it can inspect the repo but cannot modify it.

Contract:
  - Returns a pass/fail ``ReviewDecision`` with evidence-backed ``ChecklistItem``s.
  - Never emits a numeric score (1–10). The result is always bool.
  - If the session produces no parseable structured block, the decision is
    fail-closed (not pass-through) — the absence of evidence is not evidence of
    passing.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..agent.claude_backend import AgentResult, ClaudeBackend
from ..review.selfcheck import ChecklistItem
from ..core.task import Task

_REVIEW_JSON = re.compile(r"REVIEW_JSON_START\s*(.*?)\s*REVIEW_JSON_END", re.DOTALL)

_REVIEW_TURNS = 10
_DIFF_CAP = 12000  # chars — keep prompt manageable while allowing meaningful diffs
_OUTPUT_CAP = 4000


@dataclass
class ReviewDecision:
    passed: bool
    checklist: list[ChecklistItem] = field(default_factory=list)
    raw_output: str = ""

    @property
    def failed_items(self) -> list[ChecklistItem]:
        return [i for i in self.checklist if not i.passed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "items": [
                {
                    "label": i.label, "passed": i.passed,
                    "evidence": i.evidence,
                    "file": i.file, "line": i.line,
                    "comment": i.comment,
                }
                for i in self.checklist
            ],
            "raw_output": self.raw_output or None,
        }


def _git_diff(repo_path: Path, before: str = "HEAD~1", after: str = "HEAD") -> str:
    proc = subprocess.run(
        ["git", "diff", f"{before}..{after}", "--stat", "--patch", "--no-color"],
        cwd=repo_path, capture_output=True, text=True,
    )
    return (proc.stdout or "")[:_DIFF_CAP]


def _build_review_prompt(
    task: Task,
    diff: str,
    test_output: str,
    held_out_output: str,
    *,
    profile_context: str = "",
    confirmed_rules: str = "",
) -> str:
    criteria = "\n".join(f"  - {c}" for c in task.acceptance_criteria) or "  (none stated)"
    held_section = (
        f"\nHeld-out test results (tests the implementer never saw):\n{held_out_output}\n"
        if held_out_output else ""
    )
    profile_section = (
        f"\nProject profile (use these conventions as a baseline):\n{profile_context}\n"
        if profile_context else ""
    )
    rules_section = (
        f"\nConfirmed rules from past experience (the team learned these the hard way):\n"
        f"{confirmed_rules}\n"
        if confirmed_rules else ""
    )
    return (
        "You are a Staff Software Engineer performing an independent code review.\n"
        "Your ONLY job is to find flaws. Do NOT trust the implementer's work.\n"
        "Try to REFUTE the claim that this task is 'done.' Be adversarial.\n\n"
        "CRITICAL: Do NOT use any tools. Do NOT run any commands. Do NOT read any files.\n"
        "Everything you need is provided below. Respond with text ONLY.\n\n"
        f"Task: {task.title}\n"
        f"Acceptance criteria:\n{criteria}\n\n"
        f"Diff (complete and authoritative):\n```\n{diff}\n```\n\n"
        f"Test results:\n```\n{test_output or '(no test output provided)'}\n```\n"
        f"{held_section}"
        f"{profile_section}"
        f"{rules_section}\n"
        "Review the diff in THREE explicit passes. Each pass produces checklist\n"
        "items, and EVERY item must cite concrete evidence (a file:line from the\n"
        "diff, a line of command/test output, or a specific failing input).\n"
        "An item with no cited evidence is not a valid finding.\n\n"
        "PASS 1: CORRECTNESS — does the code actually meet each acceptance\n"
        "  criterion? Trace the changed code against every criterion. Does it\n"
        "  return what it claims? Are the tests real (not asserting trivia)?\n"
        "PASS 2: ARCHITECTURE — is this the right approach or a workaround? Does\n"
        "  it follow the existing patterns/conventions shown in the profile? Any\n"
        "  layering, coupling, or abstraction problems?\n"
        "PASS 3: EDGE CASES — error handling, empty/null/boundary inputs,\n"
        "  security (injection, auth, secrets), concurrency, performance.\n\n"
        "For each finding, cite the specific file:line from the diff.\n\n"
        "Rules:\n"
        "  - Do NOT use tools, run commands, or read files.\n"
        "  - Pass/fail only. No numeric scores.\n"
        "  - 'passed: true' means ALL criteria are demonstrably met.\n\n"
        "Output EXACTLY this format (and NOTHING after it):\n\n"
        "REVIEW_JSON_START\n"
        '{"passed": true_or_false, "items": [\n'
        '  {"label": "short label", "passed": true_or_false,\n'
        '   "evidence": "detailed explanation of the finding",\n'
        '   "file": "path/to/file.py", "line": 42,\n'
        '   "comment": "The review comment to post on the PR (concise, actionable)"}\n'
        "]}\n"
        "REVIEW_JSON_END\n\n"
        "For each item:\n"
        "  - 'file' must be the path exactly as shown in the diff header (e.g. 'src/foo.py')\n"
        "  - 'line' must be a line number from the RIGHT side of the diff (new file)\n"
        "  - 'comment' should be a concise, actionable PR comment suitable for posting\n"
        "  - For general observations with no specific line, set file to '' and line to 0\n"
    )


def _parse_review_output(text: str) -> ReviewDecision:
    m = _REVIEW_JSON.search(text or "")
    if not m:
        return ReviewDecision(
            passed=False,
            checklist=[ChecklistItem(
                "structured output present",
                False,
                "reviewer produced no parseable REVIEW_JSON block — fail closed",
            )],
            raw_output=text or "",
        )
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        return ReviewDecision(
            passed=False,
            checklist=[ChecklistItem("json parse", False, str(exc))],
            raw_output=text,
        )
    items = [
        ChecklistItem(
            label=str(i.get("label", "?")),
            passed=bool(i.get("passed", False)),
            evidence=str(i.get("evidence", "")),
            file=str(i.get("file", "")),
            line=int(i.get("line", 0) or 0),
            comment=str(i.get("comment", "")),
        )
        for i in (data.get("items") or [])
    ]
    # Reviewer's explicit "passed" field AND all items must agree.
    all_pass = all(i.passed for i in items)
    passed = bool(data.get("passed", False)) and all_pass
    return ReviewDecision(passed=passed, checklist=items, raw_output=text)


class AdversarialReviewer:
    """Fresh-context reviewer session — read-only, told to refute 'done.'"""

    def __init__(
        self,
        *,
        model: str = "claude-sonnet-4-6",
        backend: ClaudeBackend | None = None,
        on_event: Callable | None = None,
    ):
        self._backend = backend or ClaudeBackend(
            model=model,
            readonly=True,
        )
        self._on_event = on_event

    async def review(
        self,
        task: Task,
        *,
        repo_path: Path,
        test_output: str = "",
        held_out_output: str = "",
        before_ref: str = "HEAD~1",
        after_ref: str = "HEAD",
        diff_override: str | None = None,
        profile_context: str = "",
        confirmed_rules: str = "",
    ) -> ReviewDecision:
        diff = (diff_override[:_DIFF_CAP] if diff_override
                else _git_diff(repo_path, before_ref, after_ref))
        prompt = _build_review_prompt(
            task,
            diff,
            test_output[:_OUTPUT_CAP],
            held_out_output[:_OUTPUT_CAP],
            profile_context=profile_context,
            confirmed_rules=confirmed_rules,
        )

        # When the diff is already provided, use a single-turn call (no tools).
        # The model has everything it needs in the prompt — no repo exploration.
        if diff_override:
            return await self._fast_review(prompt, repo_path)

        # Full agent session for post-implementation reviews (needs to read files).
        return await self._agent_review(prompt, repo_path)

    async def _fast_review(self, prompt: str, repo_path: Path) -> ReviewDecision:
        """Single-turn review — diff already in prompt, no tools needed."""
        try:
            result: AgentResult = await asyncio.wait_for(
                self._backend.run(
                    prompt,
                    cwd=repo_path,
                    max_turns=1,
                    effort="medium",
                    on_event=self._on_event,
                ),
                timeout=180,
            )
        except asyncio.TimeoutError:
            return ReviewDecision(
                passed=False,
                checklist=[ChecklistItem("timeout", False,
                    "reviewer timed out after 180s — fail closed")],
            )
        return _parse_review_output(result.final_text or "")

    async def _agent_review(self, prompt: str, repo_path: Path) -> ReviewDecision:
        """Multi-turn review — model can explore the repo with read-only tools."""
        all_text_parts: list[str] = []
        original_on_event = self._on_event

        def _capture_event(event):
            if event.text:
                all_text_parts.append(event.text)
            if original_on_event:
                original_on_event(event)

        try:
            result: AgentResult = await asyncio.wait_for(
                self._backend.run(
                    prompt,
                    cwd=repo_path,
                    max_turns=_REVIEW_TURNS,
                    effort="medium",
                    on_event=_capture_event,
                ),
                timeout=300,
            )
        except asyncio.TimeoutError:
            return ReviewDecision(
                passed=False,
                checklist=[ChecklistItem("timeout", False,
                    "reviewer timed out after 300s — fail closed")],
            )
        # Try final_text first, then all captured text.
        decision = _parse_review_output(result.final_text or "")
        if not decision.passed and decision.checklist and \
                decision.checklist[0].label == "structured output present":
            full_text = "\n".join(all_text_parts)
            decision = _parse_review_output(full_text)
        return decision
