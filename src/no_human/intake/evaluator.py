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

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core.jsonparse import loads_lenient

log = logging.getLogger("no_human.evaluator")


def _render(template: str, **fields: str) -> str:
    """Substitute ``{name}`` placeholders WITHOUT ``str.format``.

    These prompt templates embed a literal JSON example (``{"verdict": ...}``).
    ``str.format`` would parse those braces as replacement fields — the field
    name ends at the ``:``, so it looks up ``kwargs['"verdict"']`` and raises
    ``KeyError('"verdict"')``, silently disabling the evaluator for every task.
    A plain ``.replace`` per field leaves all other braces untouched.
    """
    out = template
    for key, value in fields.items():
        out = out.replace("{" + key + "}", value)
    return out


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
        be = backend or ClaudeBackend(model="claude-opus-4-8", readonly=True)
        criteria_text = "\n".join(f"  - {c}" for c in acceptance_criteria) or "  (none)"
        prompt = _render(
            _EVAL_PROMPT,
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
        data = loads_lenient(m.group(1))
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


_ASSUMPTIONS_JSON = re.compile(
    r"ASSUMPTIONS_JSON_START\s*(.*?)\s*ASSUMPTIONS_JSON_END", re.DOTALL)

_ASSUMPTIONS_PROMPT = (
    "An autonomous coding agent must proceed on this task WITHOUT asking a human "
    "any questions. For each ambiguous or underspecified point, state the single "
    "most reasonable assumption the agent will proceed under. Be concrete, "
    "minimal, and prefer the interpretation a senior engineer would pick. These "
    "assumptions will be surfaced in the pull request for a human to catch at "
    "review time.\n\n"
    "Output EXACTLY:\n"
    "ASSUMPTIONS_JSON_START\n"
    '{"assumptions": ["...", "..."]}\n'
    "ASSUMPTIONS_JSON_END\n\n"
    "Task:\n"
    "Title: {title}\n"
    "Description: {description}\n"
    "Acceptance criteria:\n{criteria}\n"
)


async def resolve_assumptions(
    title: str,
    description: str,
    acceptance_criteria: list[str],
    *,
    backend: Any | None = None,
) -> list[str] | None:
    """For an ambiguous spec, return the assumptions the agent will proceed
    under so it never has to stop and ask a human (megaplan P2 / decision #1).
    Returns None on failure (advisory — never blocks the pipeline)."""
    try:
        import tempfile
        from pathlib import Path
        from ..agent.claude_backend import ClaudeBackend
        be = backend or ClaudeBackend(model="claude-opus-4-8", readonly=True)
        criteria_text = "\n".join(f"  - {c}" for c in acceptance_criteria) or "  (none)"
        prompt = _render(
            _ASSUMPTIONS_PROMPT,
            title=title,
            description=description or "(none)",
            criteria=criteria_text,
        )
        result = await be.run(prompt, max_turns=1, effort="low",
                              cwd=Path(tempfile.gettempdir()))
        m = _ASSUMPTIONS_JSON.search(result.final_text or "")
        if not m:
            return None
        data = loads_lenient(m.group(1))
        items = data.get("assumptions")
        if isinstance(items, list) and items:
            return [str(x) for x in items][:10]
        return None
    except Exception as exc:  # noqa: BLE001 — advisory, never blocks
        log.warning("assumption resolution failed (proceeding without): %s", exc)
        return None
