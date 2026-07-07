"""D1: Intake quality evaluation.

Lightweight LLM pass that classifies a task spec as one of:
  ACCEPT  — ready for implementation
  ENRICH  — good but could use stronger acceptance criteria (auto-enriched)
  CLARIFY — ambiguous, needs human input before proceeding
  DECOMPOSE — too large for a single agent session

Returns an EvalResult with verdict, quality dimensions, and optionally
enriched acceptance criteria. Advisory — never blocks the pipeline.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger("no_human.evaluator")


class EvalVerdict(str, Enum):
    ACCEPT = "accept"
    ENRICH = "enrich"
    CLARIFY = "clarify"
    DECOMPOSE = "decompose"


@dataclass
class EvalResult:
    verdict: EvalVerdict
    dimensions: dict[str, bool] = field(default_factory=dict)
    enriched_criteria: list[str] | None = None
    rationale: str = ""

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "verdict": self.verdict.value,
            "dimensions": self.dimensions,
            "rationale": self.rationale,
        }
        if self.enriched_criteria:
            d["enriched_criteria"] = self.enriched_criteria
        return d


_EVAL_JSON = re.compile(r"EVAL_JSON_START\s*(.*?)\s*EVAL_JSON_END", re.DOTALL)

_EVAL_PROMPT = (
    "You are a spec quality evaluator. Assess the task spec below and classify it.\n\n"
    "Quality dimensions (true/false for each):\n"
    "  - clear_objective: Is the goal unambiguous?\n"
    "  - testable_criteria: Are acceptance criteria concrete and verifiable?\n"
    "  - bounded_scope: Is the scope small enough for one agent session?\n"
    "  - no_missing_context: Does the spec provide enough context to start?\n\n"
    "Verdict rules:\n"
    "  - ACCEPT: all dimensions true\n"
    "  - ENRICH: clear_objective=true but testable_criteria=false — auto-generate"
    " stronger criteria\n"
    "  - CLARIFY: clear_objective=false — needs human clarification\n"
    "  - DECOMPOSE: bounded_scope=false — too large\n\n"
    "If verdict is ENRICH, include 'enriched_criteria' with improved acceptance"
    " criteria.\n\n"
    "Output EXACTLY:\n"
    "EVAL_JSON_START\n"
    '{"verdict": "accept|enrich|clarify|decompose",\n'
    ' "dimensions": {"clear_objective": bool, "testable_criteria": bool,\n'
    '               "bounded_scope": bool, "no_missing_context": bool},\n'
    ' "enriched_criteria": ["..."] or null,\n'
    ' "rationale": "one-sentence explanation"}\n'
    "EVAL_JSON_END\n\n"
    "Task:\n"
    "Title: {title}\n"
    "Description: {description}\n"
    "Acceptance criteria:\n{criteria}\n"
)


async def evaluate_spec(
    title: str,
    description: str,
    acceptance_criteria: list[str],
    *,
    backend: Any | None = None,
) -> EvalResult | None:
    """Run the intake quality evaluator. Returns None on failure (advisory)."""
    try:
        import tempfile
        from pathlib import Path
        from ..agent.claude_backend import ClaudeBackend
        be = backend or ClaudeBackend(model="claude-sonnet-4-6", readonly=True)
        criteria_text = "\n".join(f"  - {c}" for c in acceptance_criteria) or "  (none)"
        prompt = _EVAL_PROMPT.format(
            title=title,
            description=description or "(none)",
            criteria=criteria_text,
        )
        result = await be.run(prompt, max_turns=1, effort="low",
                              cwd=Path(tempfile.gettempdir()))
        text = result.final_text or ""
        m = _EVAL_JSON.search(text)
        if not m:
            log.warning("evaluator produced no parseable EVAL_JSON block")
            return None
        data = json.loads(m.group(1))
        verdict = EvalVerdict(data.get("verdict", "accept"))
        return EvalResult(
            verdict=verdict,
            dimensions=data.get("dimensions", {}),
            enriched_criteria=data.get("enriched_criteria"),
            rationale=data.get("rationale", ""),
        )
    except Exception as exc:  # noqa: BLE001 — advisory, never blocks
        log.warning("evaluator failed (proceeding without eval): %s", exc)
        return None
