"""Human-confirmed learning queue (PLAN.md 4.5).

On success → propose a skill/fact. On failure → propose an anti-pattern/rule.
Proposals are queued (``confirmed=0``) and *never* enter the active rule set
until a human confirms them in ``nh learnings`` — auto-writing rules from a
leniency-biased self-assessment would let bad lessons accumulate silently.

The active rule set (confirmed memories) is what later tasks consult; proposals
are inert until promoted.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from ..core.db import Store
from ..core.task import Task, TaskStatus

log = logging.getLogger("no_human.learning")


# Memory types (matches the migrations schema CHECK-free `type` column).
TYPE_SKILL = "skill"
TYPE_FACT = "fact"
TYPE_RULE = "rule"
TYPE_ANTI_PATTERN = "anti_pattern"


def _sig(*parts: str) -> str:
    """Stable dedupe signature so the same lesson isn't proposed twice."""
    raw = "\x1f".join(p.strip().lower() for p in parts if p)
    return "learn:" + hashlib.sha256(raw.encode()).hexdigest()[:20]


@dataclass
class Proposal:
    mem_type: str
    title: str
    content: str
    dedupe_key: str
    tags: list[str]


class LearningQueue:
    """Proposes learnings from task outcomes and manages confirmation."""

    def __init__(self, store: Store):
        self.store = store

    # ----------------------------- propose --------------------------------- #

    async def propose_from_outcome(
        self, task: Task, *, status: TaskStatus, blocker: dict[str, Any] | None = None,
        summary: str = "",
    ) -> str | None:
        """Queue a proposal derived from a terminal task outcome.

        success (awaiting_approval/done) → skill; structural blocker/failure →
        anti-pattern. Returns the new memory id, or None if deduped/not applicable.
        """
        proposal = self._build(task, status=status, blocker=blocker, summary=summary)
        if proposal is None:
            return None
        return await self.store.add_memory(
            mem_type=proposal.mem_type,
            title=proposal.title,
            content=proposal.content,
            tags=proposal.tags,
            project=task.repo_path,
            source="proposed",
            confirmed=False,
            dedupe_key=proposal.dedupe_key,
        )

    def _build(
        self, task: Task, *, status: TaskStatus, blocker: dict[str, Any] | None,
        summary: str,
    ) -> Proposal | None:
        if status in (TaskStatus.AWAITING_APPROVAL, TaskStatus.DONE):
            title = f"Approach that worked: {task.title}"[:120]
            content = (
                f"Task '{task.title}' completed to a reviewable PR.\n"
                f"{('Summary: ' + summary) if summary else ''}\n"
                "Consider capturing the successful approach as a reusable skill."
            ).strip()
            return Proposal(
                TYPE_SKILL, title, content,
                _sig("skill", task.title, task.repo_path or ""),
                tags=["success", task.source],
            )

        # Failure / structural blocker → anti-pattern (22.8).
        if blocker:
            cat = blocker.get("category", "NOVEL_UNKNOWN")
            cause = blocker.get("root_cause_hypothesis", "")
            tried = blocker.get("tried", []) or []
            title = f"Anti-pattern [{cat}]: {task.title}"[:120]
            content = (
                f"Category: {cat}\n"
                f"Root cause hypothesis: {cause}\n"
                f"Tried: {'; '.join(tried) if tried else '(none recorded)'}\n"
                "If this surprise recurs, anticipate it: capture the fix as a "
                "rule/skill so it becomes an auto-handled case (Part 22.8)."
            )
            return Proposal(
                TYPE_ANTI_PATTERN, title, content,
                _sig("anti", cat, cause or task.title, task.repo_path or ""),
                tags=["blocker", cat],
            )
        return None

    # --------------------------- confirm / list ---------------------------- #

    async def pending(self) -> list[dict[str, Any]]:
        """Proposals awaiting human confirmation."""
        return await self.store.list_memories(confirmed=False, source="proposed")

    async def active(self) -> list[dict[str, Any]]:
        """The confirmed, active rule/skill set later tasks consult."""
        return await self.store.list_memories(confirmed=True)

    async def confirm(self, mem_id: str) -> bool:
        return await self.store.confirm_memory(mem_id)

    async def reject(self, mem_id: str) -> bool:
        return await self.store.delete_memory(mem_id)
