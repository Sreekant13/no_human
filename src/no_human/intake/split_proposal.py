"""SCRUM-34: split proposal generator.

When a task blows past its safety bounds (SCOPE_EXPLOSION), a human has to
decide whether to raise the limit or split the task. This module is a single
utility-model, single-turn call that drafts 2-4 ordered sub-task proposals to
make that decision easier. It is purely advisory: it never mutates the task,
never creates tasks, and any parse failure degrades to ``None`` rather than
blocking or retrying more than once (mirrors ``evaluator.py``'s seam).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..core.jsonparse import loads_lenient
from .evaluator import UsageSink, _bounded_run, _default_utility_model, _render

log = logging.getLogger("no_human.split_proposal")

_SPLIT_JSON = re.compile(r"SPLIT_JSON_START\s*(.*?)\s*SPLIT_JSON_END", re.DOTALL)

_MIN_DRAFTS = 2
_MAX_DRAFTS = 4
_MIN_SENTENCES = 3
_MAX_SENTENCES = 5

_SPLIT_PROMPT = (
    "A coding task has grown too large for a single agent session/PR and must "
    "be split. Draft 2-4 ORDERED sub-tasks that together cover the original "
    "task, each small enough for one focused PR.\n\n"
    "For each sub-task draft, provide:\n"
    "  - title: a short imperative title\n"
    "  - description: a SELF-CONTAINED 3-5 sentence description (someone "
    "reading only this draft, with no other context, must understand what to "
    "build)\n"
    "  - contract: the interface/dependency contract this sub-task exposes to "
    "or expects from the others (what it produces, what it assumes exists)\n\n"
    "Output EXACTLY:\n"
    "SPLIT_JSON_START\n"
    '[{"title": "...", "description": "...", "contract": "..."}, ...]\n'
    "SPLIT_JSON_END\n\n"
    "Task:\n"
    "Title: {title}\n"
    "Description: {description}\n"
    "Acceptance criteria:\n{criteria}\n"
    "{files_section}"
    "{surfaces_section}"
)


def _sentence_count(text: str) -> int:
    parts = [p for p in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if p]
    return len(parts)


def _validate_drafts(data: Any) -> list[dict[str, str]] | None:
    if not isinstance(data, list) or not (_MIN_DRAFTS <= len(data) <= _MAX_DRAFTS):
        return None
    drafts: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            return None
        title = str(item.get("title") or "").strip()
        description = str(item.get("description") or "").strip()
        contract = str(item.get("contract") or "").strip()
        if not title or not contract:
            return None
        n = _sentence_count(description)
        if not (_MIN_SENTENCES <= n <= _MAX_SENTENCES):
            return None
        drafts.append({"title": title, "description": description, "contract": contract})
    return drafts


def _render_proposal(drafts: list[dict[str, str]]) -> str:
    lines = ["Split proposal:"]
    for i, d in enumerate(drafts, start=1):
        lines.append(f"\n{i}. {d['title']}")
        lines.append(d["description"])
        lines.append(f"Contract: {d['contract']}")
    return "\n".join(lines)


async def generate_split_proposal(
    task: Any,
    files_to_change: list[str] | None = None,
    surfaces: Any = None,
    *,
    backend: Any | None = None,
    model: str | None = None,
    usage_sink: UsageSink | None = None,
) -> str | None:
    """Draft a 2-4 sub-task split proposal for an over-scope task.

    Utility-model, single-turn, strictly-parsed. Returns ``None`` on any
    failure (missing block, bad JSON, wrong draft count, bad sentence count,
    missing contract, backend exception) — a missing proposal degrades a
    hint, it never blocks or retries beyond the single parse retry below.
    """
    try:
        import tempfile
        from pathlib import Path
        from ..agent.advisory import advisory_backend

        be = backend or advisory_backend(
            model or _default_utility_model(), role="intake",
        )
        criteria = getattr(task, "acceptance_criteria", None) or []
        criteria_text = "\n".join(f"  - {c}" for c in criteria) or "  (none)"
        files_section = (
            f"Files to change:\n" + "\n".join(f"  - {f}" for f in files_to_change) + "\n"
            if files_to_change else ""
        )
        surfaces_section = f"Surfaces: {surfaces}\n" if surfaces else ""
        prompt = _render(
            _SPLIT_PROMPT,
            title=getattr(task, "title", "") or "",
            description=getattr(task, "description", None) or "(none)",
            criteria=criteria_text,
            files_section=files_section,
            surfaces_section=surfaces_section,
        )
        cwd = Path(tempfile.gettempdir())
        result = await _bounded_run(be, prompt, max_turns=1, effort="low", cwd=cwd,
                                    usage_sink=usage_sink)
        m = _SPLIT_JSON.search(result.final_text or "")
        if not m:
            log.warning("split proposal produced no parseable SPLIT_JSON "
                        "block; retrying once")
            # The retry is billed too — _bounded_run books both calls.
            result = await _bounded_run(be, prompt, max_turns=1, effort="low", cwd=cwd,
                                        usage_sink=usage_sink)
            m = _SPLIT_JSON.search(result.final_text or "")
        if not m:
            log.warning("split proposal produced no parseable SPLIT_JSON block")
            return None
        data = loads_lenient(m.group(1))
        drafts = _validate_drafts(data)
        if drafts is None:
            log.warning("split proposal failed strict validation")
            return None
        return _render_proposal(drafts)
    except Exception as exc:  # noqa: BLE001 — advisory, never blocks
        log.warning("split proposal failed: %s", type(exc).__name__)
        return None
