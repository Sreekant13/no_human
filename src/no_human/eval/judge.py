"""Intent-match judge (PLAN.md 21.2).

A *different-model* LLM judge compares the agent's diff to the known-good diff.
It must **cite evidence** and is calibrated against leniency bias — we never
read a bare "9/10". The verdict is a boolean match + cited evidence, parsed from
a fenced JUDGE_JSON block and **failing closed** (no block → not a match), the
same discipline as the adversarial reviewer.

The judge is injectable so the eval harness runs offline in tests; the default
uses the review model (claude-opus-4-8) — deliberately a *different* model
from the implementer (claude-sonnet-5).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_JUDGE_JSON = re.compile(r"JUDGE_JSON_START\s*(.*?)\s*JUDGE_JSON_END", re.DOTALL)


@dataclass
class JudgeVerdict:
    match: bool
    evidence: str = ""
    raw_output: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"match": self.match, "evidence": self.evidence}


def build_judge_prompt(task_title: str, criteria: list[str],
                       agent_diff: str, known_good_diff: str) -> str:
    crit = "\n".join(f"  - {c}" for c in criteria) or "  (none stated)"
    return (
        "You are an impartial evaluation judge. Compare an AGENT diff against a "
        "KNOWN-GOOD reference diff for the same task. Decide whether the agent's "
        "change achieves the SAME INTENT (it need not be textually identical — "
        "different but correct implementations count as a match).\n\n"
        "Calibration: do not be lenient. If the agent's change omits a required "
        "behavior, breaks something, or only partially satisfies the criteria, "
        "it is NOT a match. Cite specific lines/hunks as evidence — never give a "
        "bare score.\n\n"
        f"Task: {task_title}\n"
        f"Acceptance criteria:\n{crit}\n\n"
        f"=== KNOWN-GOOD DIFF ===\n{known_good_diff[:8000]}\n\n"
        f"=== AGENT DIFF ===\n{agent_diff[:8000]}\n\n"
        "Emit your verdict between markers exactly:\n"
        "JUDGE_JSON_START\n"
        '{"match": true|false, "evidence": "cite the specific hunks/lines that '
        'justify the verdict"}\n'
        "JUDGE_JSON_END"
    )


def parse_verdict(text: str) -> JudgeVerdict:
    """Parse the judge's output. Fail closed: no parseable block → not a match."""
    if not text:
        return JudgeVerdict(False, "judge produced no output", text)
    m = _JUDGE_JSON.search(text)
    if not m:
        return JudgeVerdict(False, "no JUDGE_JSON block found", text)
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return JudgeVerdict(False, "malformed JUDGE_JSON", text)
    return JudgeVerdict(
        match=bool(data.get("match", False)),
        evidence=str(data.get("evidence", "")),
        raw_output=text,
    )


@dataclass
class GoalVerdict:
    satisfied: bool
    evidence: str = ""
    raw_output: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"satisfied": self.satisfied, "evidence": self.evidence}


def build_goal_prompt(request: str, criteria: list[str], agent_diff: str,
                      outcome_status: str, report: str = "") -> str:
    crit = "\n".join(f"  - {c}" for c in criteria) or "  (none stated)"
    return (
        "You are an impartial evaluation judge. A developer made this request "
        "to an autonomous coding system:\n\n"
        f"=== REQUEST ===\n{request[:6000]}\n\n"
        f"=== ACCEPTANCE CRITERIA (may be empty) ===\n{crit}\n\n"
        f"The system finished with status: {outcome_status}\n"
        "Its complete change as a diff:\n\n"
        f"=== AGENT DIFF ===\n{agent_diff[:10000] or '(no file changes)'}\n\n"
        + (f"=== AGENT REPORT (the deliverable for questions, investigations, "
           f"code reviews and design docs — an EMPTY DIFF IS CORRECT for these "
           f"kinds) ===\n{report[:10000]}\n\n" if report else "")
        + "Decide whether the work SATISFIES WHAT WAS ASKED. Judge the "
        "DELIVERABLE THE REQUEST ASKED FOR: a code change is judged on the "
        "diff; a question, investigation, or code review is judged on the "
        "report — do NOT require a diff for those. You have read "
        "access to the repository at the current path — verify claims and "
        "cite the files/lines you actually checked. Calibration: do not be "
        "lenient; a partial, broken, or off-target change is NOT satisfied. "
        "Never give a numeric score.\n\n"
        "Emit your verdict between markers exactly:\n"
        "JUDGE_JSON_START\n"
        '{"satisfied": true|false, "evidence": "cited files/lines and the '
        'reasons"}\n'
        "JUDGE_JSON_END"
    )


def parse_goal_verdict(text: str) -> GoalVerdict:
    """Fail closed: no parseable JUDGE_JSON block → not satisfied."""
    if not text:
        return GoalVerdict(False, "judge produced no output", text)
    m = _JUDGE_JSON.search(text)
    if not m:
        return GoalVerdict(False, "no JUDGE_JSON block found", text)
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return GoalVerdict(False, "malformed JUDGE_JSON", text)
    return GoalVerdict(
        satisfied=bool(data.get("satisfied", False)),
        evidence=str(data.get("evidence", "")),
        raw_output=text,
    )


class GoalJudge:
    """North-star bench judge: did the result reach what the operator asked?

    Deliberately sees ONLY the spec's request/criteria and the agent's diff —
    never the source transcript or the original solution (no-cheating
    invariant). Same fail-closed JUDGE_JSON discipline as IntentJudge; runs on
    the review model (different from the implementer)."""

    def __init__(self, *, model: str = "claude-opus-4-8", backend: Any | None = None):
        self.model = model
        self._backend = backend

    def _ensure_backend(self) -> Any:
        if self._backend is None:
            from ..agent.claude_backend import ClaudeBackend
            self._backend = ClaudeBackend(model=self.model, readonly=True)
        return self._backend

    async def judge(
        self, *, request: str, criteria: list[str], agent_diff: str,
        outcome_status: str, repo_path: str | None = None, report: str = "",
    ) -> GoalVerdict:
        prompt = build_goal_prompt(request, criteria, agent_diff,
                                   outcome_status, report=report)
        backend = self._ensure_backend()
        result = await backend.run(
            prompt, cwd=repo_path, max_turns=10, effort="high")
        return parse_goal_verdict(getattr(result, "final_text", "") or "")


class IntentJudge:
    """Runs the different-model judge over (agent_diff, known_good_diff)."""

    def __init__(self, *, model: str = "claude-opus-4-8", backend: Any | None = None):
        self.model = model
        self._backend = backend  # lazily constructed to avoid SDK import at module load

    def _ensure_backend(self) -> Any:
        if self._backend is None:
            from ..agent.claude_backend import ClaudeBackend
            self._backend = ClaudeBackend(model=self.model, readonly=True)
        return self._backend

    async def judge(
        self, *, task_title: str, criteria: list[str], agent_diff: str,
        known_good_diff: str, repo_path: str | None = None,
    ) -> JudgeVerdict:
        if not known_good_diff:
            # No reference to compare against → cannot assert a match.
            return JudgeVerdict(False, "no known-good diff provided")
        prompt = build_judge_prompt(task_title, criteria, agent_diff, known_good_diff)
        backend = self._ensure_backend()
        result = await backend.run(
            prompt, cwd=repo_path, max_turns=10, effort="high")
        return parse_verdict(getattr(result, "final_text", "") or "")
