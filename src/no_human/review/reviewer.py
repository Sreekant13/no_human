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

_REVIEW_TURNS = 20
_DIFF_CAP = 8000   # chars — keep prompt manageable
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
            "items": [{"label": i.label, "passed": i.passed, "evidence": i.evidence}
                      for i in self.checklist],
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
) -> str:
    criteria = "\n".join(f"  - {c}" for c in task.acceptance_criteria) or "  (none stated)"
    held_section = (
        f"\nHeld-out test results (tests the implementer never saw):\n{held_out_output}\n"
        if held_out_output else ""
    )
    return (
        "You are an independent code reviewer. Your ONLY job is to find flaws.\n"
        "Do NOT trust the implementer's work. Try to refute the claim that this task is 'done.'\n\n"
        f"Task: {task.title}\n"
        f"Acceptance criteria:\n{criteria}\n\n"
        f"Diff (changes introduced by this implementation):\n```\n{diff}\n```\n\n"
        f"Test results from the local suite:\n```\n{test_output}\n```\n"
        f"{held_section}\n"
        "Instructions:\n"
        "  1. Examine EACH acceptance criterion. Verify it is ACTUALLY satisfied — read\n"
        "     the changed code, do not trust the commit message or summary.\n"
        "  2. Check for: incorrect logic, missing edge cases, tests that pass vacuously,\n"
        "     test weakening (assertions removed or made weaker), scope drift.\n"
        "  3. Use your Bash/Read/Grep tools to investigate. Run 'git diff HEAD~1..HEAD'\n"
        "     yourself if you want to verify the diff shown above.\n"
        "  4. For each checklist item, cite CONCRETE evidence: file:line, or a snippet\n"
        "     from the test output.\n\n"
        "IMPORTANT rules:\n"
        "  - Do NOT modify any files. You are read-only.\n"
        "  - Do NOT produce a numeric score (1–10). The result is ALWAYS pass/fail.\n"
        "  - Be adversarial. Assume 'not done' until you have concrete proof otherwise.\n"
        "  - 'passed: true' means ALL criteria are demonstrably met with evidence.\n\n"
        "After your investigation, output the following block (and NOTHING after it):\n\n"
        "REVIEW_JSON_START\n"
        '{"passed": true_or_false, "items": [\n'
        '  {"label": "short label", "passed": true_or_false, "evidence": "file:line or snippet"}\n'
        "]}\n"
        "REVIEW_JSON_END\n"
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
    ) -> ReviewDecision:
        diff = _git_diff(repo_path, before_ref, after_ref)
        prompt = _build_review_prompt(
            task,
            diff,
            test_output[:_OUTPUT_CAP],
            held_out_output[:_OUTPUT_CAP],
        )
        result: AgentResult = await self._backend.run(
            prompt,
            cwd=repo_path,
            max_turns=_REVIEW_TURNS,
            effort="high",
            on_event=self._on_event,
        )
        return _parse_review_output(result.final_text or "")
