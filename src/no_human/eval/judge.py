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

import asyncio
import json
import logging
import re
from types import SimpleNamespace
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


#: Fail-closed evidence strings that signal a TRANSIENT judge reply (empty or
#: truncated before the JUDGE_JSON block) — the quota-stress "Stream closed"
#: signature. GoalJudge.judge retries once on these OR on a backend error flag
#: (`AgentResult.is_error`). "malformed JUDGE_JSON" is deliberately excluded: a
#: block was present, so the judge answered — retrying a format bug just doubles
#: cost.
_JUDGE_TRANSIENT = ("judge produced no output", "no JUDGE_JSON block found")

#: Backoff before the single retry. The transient IS quota saturation, so an
#: instant re-issue tends to reproduce the truncation (review D1) — a short pause
#: lets the blip clear. Module-level so tests set it to 0. Far below the CI
#: infra-retry's 2 min: this is one LLM call inside a multi-hour run, not a pod
#: reschedule.
_RETRY_BACKOFF_S = 15.0
# Hard wall-clock ceiling on a judge LLM call — a hung SDK subprocess
# (Stream-closed transport) must not wedge the run forever (max_turns=10,
# effort=high is heavy, so this is generous; it only bounds hangs).
_JUDGE_TIMEOUT_S = 600.0
log = logging.getLogger("no_human.judge")


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
        # Retry ONCE on an empty / block-less reply — the quota-stress signature
        # ("Stream closed" truncates the judge mid-reply, observed in
        # expanded-core-v3). A transient blip must not fail-close a spec and lose
        # its measurement (mirrors the CI infra-retry policy). A genuine
        # block-less reply persists and still fails closed after the retry; a
        # malformed-JSON reply is NOT retried — the judge answered substantively,
        # so that's a format bug, not a transient.
        verdict = None
        for attempt in range(2):
            try:
                result = await asyncio.wait_for(
                    backend.run(prompt, cwd=repo_path, max_turns=10,
                                effort="high"),
                    timeout=_JUDGE_TIMEOUT_S)
            except (asyncio.TimeoutError, TimeoutError):
                # A hung SDK subprocess (Stream-closed) must not wedge the judge
                # forever — surface a transient error-result so the existing
                # retry-once-then-fail-closed path below handles it.
                log.warning("judge backend.run timed out after %ss (attempt %d)",
                            _JUDGE_TIMEOUT_S, attempt)
                result = SimpleNamespace(final_text="", is_error=True)
            verdict = parse_goal_verdict(getattr(result, "final_text", "") or "")
            # Transient = the backend flagged an error (the structured signal) OR
            # the reply was empty / truncated before the JUDGE_JSON block.
            transient = (getattr(result, "is_error", False)
                         or verdict.evidence in _JUDGE_TRANSIENT)
            if not transient:
                return verdict
            if attempt == 0:            # back off once, before the single retry
                await asyncio.sleep(_RETRY_BACKOFF_S)
        return verdict


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
        # Same transient-retry as GoalJudge (review D3): a Stream-closed / empty
        # reply must not silently fail-close the intent-match eval either.
        verdict = None
        for attempt in range(2):
            try:
                result = await asyncio.wait_for(
                    backend.run(prompt, cwd=repo_path, max_turns=10,
                                effort="high"),
                    timeout=_JUDGE_TIMEOUT_S)
            except (asyncio.TimeoutError, TimeoutError):
                # A hung SDK subprocess (Stream-closed) must not wedge the judge
                # forever — surface a transient error-result so the existing
                # retry-once-then-fail-closed path below handles it.
                log.warning("judge backend.run timed out after %ss (attempt %d)",
                            _JUDGE_TIMEOUT_S, attempt)
                result = SimpleNamespace(final_text="", is_error=True)
            verdict = parse_verdict(getattr(result, "final_text", "") or "")
            transient = (getattr(result, "is_error", False)
                         or verdict.evidence in _JUDGE_TRANSIENT)
            if not transient:
                return verdict
            if attempt == 0:
                await asyncio.sleep(_RETRY_BACKOFF_S)
        return verdict
