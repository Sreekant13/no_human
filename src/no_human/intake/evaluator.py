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


def _default_utility_model() -> str:
    """The configured default, read from the schema rather than a literal.

    Both entry points below used to hardcode ``claude-opus-4-8``, so a config
    change could never reach them. Callers that hold a resolved config should
    pass ``model=`` explicitly; this is the floor, not the policy.
    """
    from ..config import DEFAULT_CONFIG
    return DEFAULT_CONFIG["llm"]["utility_model"]


async def evaluate_spec(
    title: str,
    description: str,
    acceptance_criteria: list[str],
    *,
    backend: Any | None = None,
    model: str | None = None,
) -> EvalResult | None:
    """Run the intake quality evaluator. Returns None on failure (advisory)."""
    try:
        import tempfile
        from pathlib import Path
        from ..agent.claude_backend import ClaudeBackend
        be = backend or ClaudeBackend(
            model=model or _default_utility_model(), readonly=True,
        )
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
    model: str | None = None,
) -> list[str] | None:
    """For an ambiguous spec, return the assumptions the agent will proceed
    under so it never has to stop and ask a human (megaplan P2 / decision #1).
    Returns None on failure (advisory — never blocks the pipeline)."""
    try:
        import tempfile
        from pathlib import Path
        from ..agent.claude_backend import ClaudeBackend
        be = backend or ClaudeBackend(
            model=model or _default_utility_model(), readonly=True,
        )
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


# ------------------------- intake grill (every task) ----------------------- #
# Operator directive 2026-07-17: every task goes through an intake grill "like
# with a real user". Questions are generated with an EVPI-style rubric (ask
# only what changes a decision — SAGE-Agent, ACL 2026); when no human is
# present they are answered from repo evidence as documented reversible
# assumptions, and the full Q&A is surfaced at the human gate. Advisory
# end-to-end: any failure returns what it has and never blocks the pipeline.

_GRILL_JSON = re.compile(r"GRILL_JSON_START\s*(.*?)\s*GRILL_JSON_END", re.DOTALL)

_GRILL_QUESTIONS_PROMPT = (
    "You are the intake clarifier for an autonomous coding agent. A real "
    "requester filed this task and has walked away. Generate the clarifying "
    "questions a careful engineer would ask the requester BEFORE planning.\n\n"
    "Rules:\n"
    "- Ask ONLY questions whose answer changes what gets built (target file/"
    "repo, scope, deliverable artifact, acceptance ambiguity). For each "
    "question state the decision its answer changes. Output NO question whose "
    "answer would not change the work.\n"
    "- Tag carve_out: \"access\" for anything needing credentials/permissions/"
    "external accounts, \"destructive\" for anything irreversible (deletes, "
    "rotations, prod mutations), else \"none\". Carve-out questions are for a "
    "human — never answered autonomously.\n"
    "- At most 8 questions; fewer is better. A crisp spec deserves zero.\n\n"
    "Output EXACTLY:\n"
    "GRILL_JSON_START\n"
    '{"questions": [{"question": "...", "decision_it_changes": "...", '
    '"carve_out": "none|access|destructive"}]}\n'
    "GRILL_JSON_END\n\n"
    "Task:\n"
    "Title: {title}\n"
    "Description: {description}\n"
    "Acceptance criteria:\n{criteria}\n"
)


@dataclass
class GrillQA:
    """One intake question with its (eventual) answer."""

    question: str
    decision_it_changes: str
    answer: str = ""
    source: str = ""  # "human" | "repo-evidence" | "assumption"
    carve_out: str = "none"  # "none" | "access" | "destructive"

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "decision_it_changes": self.decision_it_changes,
            "answer": self.answer,
            "source": self.source,
            "carve_out": self.carve_out,
        }


async def generate_grill_questions(
    title: str,
    description: str,
    acceptance_criteria: list[str],
    *,
    backend: Any | None = None,
    model: str | None = None,
) -> list[GrillQA] | None:
    """Generate the intake grill's questions. None on failure (advisory)."""
    try:
        import tempfile
        from pathlib import Path
        from ..agent.claude_backend import ClaudeBackend
        be = backend or ClaudeBackend(
            model=model or _default_utility_model(), readonly=True,
        )
        criteria_text = "\n".join(f"  - {c}" for c in acceptance_criteria) or "  (none)"
        prompt = _render(
            _GRILL_QUESTIONS_PROMPT,
            title=title,
            description=description or "(none)",
            criteria=criteria_text,
        )
        result = await be.run(prompt, max_turns=1, effort="low",
                              cwd=Path(tempfile.gettempdir()))
        m = _GRILL_JSON.search(result.final_text or "")
        if not m:
            log.warning("grill produced no parseable GRILL_JSON block")
            return None
        data = loads_lenient(m.group(1))
        items = data.get("questions")
        if not isinstance(items, list) or not items:
            return None
        out: list[GrillQA] = []
        for item in items[:8]:
            if not isinstance(item, dict) or not item.get("question"):
                continue
            carve = str(item.get("carve_out", "none"))
            out.append(GrillQA(
                question=str(item["question"]),
                decision_it_changes=str(item.get("decision_it_changes", "")),
                carve_out=carve if carve in ("none", "access", "destructive")
                else "none",
            ))
        return out or None
    except Exception as exc:  # noqa: BLE001 — advisory, never blocks
        log.warning("grill question generation failed (proceeding): %s", exc)
        return None


_GRILL_ANSWERS = re.compile(
    r"GRILL_ANSWERS_START\s*(.*?)\s*GRILL_ANSWERS_END", re.DOTALL)

_GRILL_ANSWERS_PROMPT = (
    "You are answering intake questions for an autonomous coding agent, using "
    "ONLY this repository's contents and the task spec below. The requester is "
    "not available. For each question, give the single most reasonable "
    "REVERSIBLE answer a senior engineer would proceed under, citing repo "
    "evidence as path:line where you found it. If the repo holds no evidence, "
    "answer with the minimal reasonable assumption and source \"assumption\". "
    "Never invent evidence.\n"
    "You have a LIMITED number of turns: explore at most a few files, and "
    "RESERVE YOUR FINAL TURN for the output block. The block is REQUIRED — "
    "partial or assumption answers are fine, but never end without emitting "
    "it.\n\n"
    "Output EXACTLY:\n"
    "GRILL_ANSWERS_START\n"
    '{"answers": [{"i": 0, "answer": "...", "source": '
    '"repo-evidence|assumption"}]}\n'
    "GRILL_ANSWERS_END\n\n"
    "Task:\n"
    "Title: {title}\n"
    "Description: {description}\n"
    "Acceptance criteria:\n{criteria}\n\n"
    "Questions:\n{questions}\n"
)


async def grill_spec(
    title: str,
    description: str,
    acceptance_criteria: list[str],
    repo_path: Any | None,
    *,
    backend: Any | None = None,
    model: str | None = None,
    questions: list[GrillQA] | None = None,
) -> list[GrillQA] | None:
    """The full unattended grill: generate questions, answer the answerable
    ones FROM THE REPO (the answering session's cwd is the task's repo — the
    pre-existing resolve_assumptions path is repo-blind), and hard-gate the
    carve-outs for a human. Never raises; a failed answering pass returns the
    questions unanswered rather than fabricating."""
    try:
        qs = questions or await generate_grill_questions(
            title, description, acceptance_criteria,
            backend=backend, model=model,
        )
        if not qs:
            return None
        for q in qs:
            if q.carve_out != "none":
                q.answer = "HUMAN-GATED: not self-answerable"
                q.source = ""
        answerable = [(i, q) for i, q in enumerate(qs) if q.carve_out == "none"]
        if not answerable:
            return qs
        try:
            import tempfile
            from pathlib import Path
            from ..agent.claude_backend import ClaudeBackend
            be = backend or ClaudeBackend(
                model=model or _default_utility_model(), readonly=True,
            )
            criteria_text = ("\n".join(f"  - {c}" for c in acceptance_criteria)
                             or "  (none)")
            q_text = "\n".join(
                f"  {i}. {q.question} (decides: {q.decision_it_changes})"
                for i, q in answerable)
            prompt = _render(
                _GRILL_ANSWERS_PROMPT,
                title=title,
                description=description or "(none)",
                criteria=criteria_text,
                questions=q_text,
            )
            cwd = Path(repo_path) if repo_path else Path(tempfile.gettempdir())
            result = await be.run(prompt, max_turns=8, effort="low", cwd=cwd)
            m = _GRILL_ANSWERS.search(result.final_text or "")
            if not m:
                # v10 drill: the 8-turn session can spend itself exploring
                # and end blockless — retry ONCE (2/2 budget burns traced to
                # exactly this silent empty).
                log.warning("grill answering emitted no block; retrying once")
                result = await be.run(prompt, max_turns=8, effort="low",
                                      cwd=cwd)
                m = _GRILL_ANSWERS.search(result.final_text or "")
            if m:
                data = loads_lenient(m.group(1))
                answerable_idx = {i for i, _ in answerable}
                for item in data.get("answers", []) or []:
                    if not isinstance(item, dict):
                        continue
                    try:
                        i = int(item.get("i"))
                    except (TypeError, ValueError):
                        continue
                    # The model may only answer the questions it was asked —
                    # a carve-out index in its output is ignored.
                    if i in answerable_idx and item.get("answer"):
                        qs[i].answer = str(item["answer"])[:400]
                        src = str(item.get("source", "assumption"))
                        qs[i].source = (src if src in
                                        ("repo-evidence", "assumption")
                                        else "assumption")
        except Exception as exc:  # noqa: BLE001 — unanswered beats broken
            log.warning("grill answering failed (questions unanswered): %s", exc)
        return qs
    except Exception as exc:  # noqa: BLE001 — advisory, never blocks
        log.warning("grill failed entirely (proceeding without): %s", exc)
        return None
