"""Context model + the binary completeness check (PLAN.md 4.2).

The completeness check is a binary presence test on named artifacts — do we have
the acceptance criteria, a target repo, and at least one worked example/doc — NOT
a 1–10 "context score" (numeric self-scores are unreliable; see PLAN.md Part 5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..core.task import Task


@dataclass
class ContextChunk:
    source: str            # codebase | teams | outlook | sessions | ...
    title: str
    content: str
    ref: str = ""          # url / path / id
    score: float = 0.0     # relevance hint (ordering only, never a gate)


@dataclass
class CompletenessReport:
    ok: bool
    present: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


@dataclass
class TaskContext:
    chunks: list[ContextChunk] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)   # source -> error
    completeness: CompletenessReport | None = None

    def by_source(self, source: str) -> list[ContextChunk]:
        return [c for c in self.chunks if c.source == source]

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunks": [
                {"source": c.source, "title": c.title, "ref": c.ref,
                 "content": c.content[:4000], "score": c.score}
                for c in self.chunks
            ],
            "errors": self.errors,
            "completeness": (
                {"ok": self.completeness.ok, "present": self.completeness.present,
                 "missing": self.completeness.missing}
                if self.completeness else None
            ),
        }


class ContextSource(Protocol):
    name: str

    async def gather(self, task: Task) -> list[ContextChunk]:
        ...


def check_completeness(task: Task, ctx: TaskContext) -> CompletenessReport:
    """Binary presence test on the artifacts needed to start work safely."""
    present: list[str] = []
    missing: list[str] = []

    (present if task.acceptance_criteria else missing).append("acceptance_criteria")
    (present if task.repo_path else missing).append("target_repo")

    has_example = any(
        c.source in ("codebase", "teams", "outlook", "sessions") for c in ctx.chunks
    )
    (present if has_example else missing).append("worked_example_or_doc")

    return CompletenessReport(ok=not missing, present=present, missing=missing)


def keywords(task: Task, limit: int = 12) -> list[str]:
    """Extract search keywords from a task: identifiers + the external id."""
    text = f"{task.title} {task.description or ''}"
    # code-ish identifiers, dotted paths, CamelCase, snake_case
    toks = re.findall(r"[A-Za-z_][A-Za-z0-9_./-]{2,}", text)
    stop = {"the", "and", "for", "with", "via", "from", "this", "that", "should",
            "create", "add", "update", "data", "service"}
    seen: list[str] = []
    if task.external_id:
        seen.append(task.external_id)
    for t in toks:
        tl = t.lower()
        if tl in stop or len(t) < 4 or t in seen:
            continue
        seen.append(t)
        if len(seen) >= limit:
            break
    return seen
