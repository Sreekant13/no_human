"""Intent-match judge (PLAN.md 21.2).

A *different-model* LLM judge compares the agent's diff to the known-good diff.
It must **cite evidence** and is calibrated against leniency bias — we never
read a bare "9/10". The verdict is a boolean match + cited evidence, parsed from
a fenced JUDGE_JSON block and **failing closed** (no block → not a match), the
same discipline as the adversarial reviewer.

The judge is injectable so the eval harness runs offline in tests; the default
uses the review model (claude-sonnet-4-6) — deliberately a *different* model
from the implementer (claude-opus-4-8).
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


class IntentJudge:
    """Runs the different-model judge over (agent_diff, known_good_diff)."""

    def __init__(self, *, model: str = "claude-sonnet-4-6", backend: Any | None = None):
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
